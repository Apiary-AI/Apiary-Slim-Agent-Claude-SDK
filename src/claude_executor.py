"""Queue-based worker that invokes Claude Agent SDK and routes output."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKError, Message, query
from claude_code_sdk._internal import client as _sdk_client
from claude_code_sdk._internal import message_parser
from claude_code_sdk.types import AssistantMessage, ResultMessage, SystemMessage

# Patch parse_message to handle unknown message types (e.g. rate_limit_event)
# instead of crashing the stream. Must patch in both modules since client.py
# imports it directly.
_original_parse = message_parser.parse_message


def _patched_parse(data: dict) -> Message:
    try:
        return _original_parse(data)
    except Exception:
        return SystemMessage(subtype=data.get("type", "unknown"), data=data)


message_parser.parse_message = _patched_parse
_sdk_client.parse_message = _patched_parse
from telegram import Bot

from .apiary_client import ApiaryClient
from .config import Config
from .module_loader import collect_mcp_servers, discover_modules
from .telegram_streamer import TelegramStreamer

log = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "apiary"
    apiary_task_id: str | None = None


_modules = discover_modules()
_mcp = collect_mcp_servers(_modules)


class ClaudeExecutor:
    def __init__(
        self,
        config: Config,
        apiary: ApiaryClient | None,
        bot: Bot,
    ) -> None:
        self._config = config
        self._apiary = apiary
        self._bot = bot
        self.queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    async def run(self) -> None:
        """Infinite loop: pull requests from queue, execute one at a time."""
        log.info("Claude executor started")
        while True:
            req = await self.queue.get()
            try:
                await self._execute(req)
            except Exception:
                log.exception("Execution failed for request: %s", req)
            finally:
                self.queue.task_done()

    async def _report_progress(self, task_id: str, interval: int = 30) -> None:
        """Send periodic progress updates to keep the Apiary task alive."""
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._apiary.update_progress(task_id, progress)
            except Exception:
                log.debug("Progress update failed for task %s", task_id)

    async def _execute(self, req: ExecutionRequest, retries: int = 3) -> None:
        streamer = TelegramStreamer(self._bot, req.chat_id)
        await streamer.start()
        t0 = time.monotonic()
        full_text = ""

        # Start background progress reporter for Apiary tasks
        progress_task: asyncio.Task | None = None
        if req.source == "apiary" and req.apiary_task_id and self._apiary:
            progress_task = asyncio.create_task(
                self._report_progress(req.apiary_task_id)
            )

        try:
            await self._execute_inner(req, streamer, retries)
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

    async def _execute_inner(
        self, req: ExecutionRequest, streamer: TelegramStreamer, retries: int,
    ) -> None:
        full_text = ""

        for attempt in range(1, retries + 1):
            try:
                options = ClaudeCodeOptions(
                    model=self._config.claude_model,
                    max_turns=self._config.claude_max_turns,
                    permission_mode="bypassPermissions",
                    cwd=self._config.claude_working_dir,
                    **({"mcp_servers": _mcp} if _mcp else {}),
                )
                async for message in query(
                    prompt=req.prompt,
                    options=options,
                ):
                    text = self._extract_text(message)
                    if text:
                        full_text += text
                        await streamer.append(text)

                    tool_info = self._extract_tool_use(message)
                    if tool_info:
                        await streamer.send_tool_notification(*tool_info)

                await streamer.finish()

                # Complete Apiary task if applicable
                if req.source == "apiary" and req.apiary_task_id and self._apiary:
                    result = full_text[-2000:] if len(full_text) > 2000 else full_text
                    try:
                        await self._apiary.complete_task(req.apiary_task_id, result)
                    except Exception:
                        log.warning(
                            "Failed to complete apiary task %s — claim may have expired",
                            req.apiary_task_id, exc_info=True,
                        )
                return

            except (ClaudeSDKError, Exception) as e:
                err_str = str(e)
                is_rate_limit = "rate_limit" in err_str.lower()

                if is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning("Rate limited (attempt %d/%d), retrying in %ds", attempt, retries, wait)
                    await streamer.append(f"\n⏳ Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

                if isinstance(e, ClaudeSDKError):
                    log.error("Claude SDK error: %s", e)
                else:
                    log.exception("Unexpected error during execution")
                await streamer.error(f"Error: {e}")
                if req.source == "apiary" and req.apiary_task_id and self._apiary:
                    try:
                        await self._apiary.fail_task(req.apiary_task_id, err_str)
                    except Exception:
                        log.warning("Failed to mark apiary task %s as failed", req.apiary_task_id)
                return

    @staticmethod
    def _extract_text(message: Message) -> str:
        """Extract assistant text from a Claude SDK message.

        Only extract from AssistantMessage — ResultMessage contains
        a duplicate of the already-streamed text.
        """
        if isinstance(message, AssistantMessage):
            parts = []
            for block in message.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
            return "".join(parts)
        return ""

    @staticmethod
    def _extract_tool_use(message: Message) -> tuple[str, object] | None:
        """Extract tool use info if present."""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "name") and hasattr(block, "input"):
                    return (block.name, block.input)
        return None
