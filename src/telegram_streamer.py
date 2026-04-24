"""Streams Claude output to Telegram by editing messages in real-time.

All Telegram API calls are delegated to a :class:`TelegramGateway` instance
which serializes them through a single processing loop.  This class handles
only buffer management, markdown formatting, and message tracking.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

from .redactor import redact
from .telegram_gateway import TelegramGateway

log = logging.getLogger(__name__)

# Telegram message text limit (leave margin for formatting)
MAX_MSG_LEN = 4000
MIN_EDIT_INTERVAL = 1.5  # seconds between edits (per-streamer)

# ── Human-readable tool descriptions ─────────────────────────────────

_TOOL_LABELS: dict[str, str] = {
    "Bash": "Running command",
    "Read": "Reading",
    "Write": "Writing",
    "Edit": "Editing",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching page",
    "Agent": "Running sub-agent",
    "NotebookEdit": "Editing notebook",
}


def _humanize_tool(tool_name: str, tool_input: Any) -> str:
    """Create a human-readable one-liner for a tool invocation."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")

    detail = ""
    if tool_name == "Bash":
        # Show the whole command (truncated below) — splitting at &&/| hid
        # the actual work after a leading `cd`, so the user saw "cd" for
        # several minutes while a test suite was running.
        detail = " ".join(inp.get("command", "").split())
    elif tool_name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "")
        if path:
            detail = path.rsplit("/", 1)[-1]
    elif tool_name == "Glob":
        detail = inp.get("pattern", "")
    elif tool_name == "Grep":
        detail = inp.get("pattern", "")
    elif tool_name == "WebSearch":
        detail = inp.get("query", "")
    elif tool_name == "WebFetch":
        detail = inp.get("url", "")
    elif tool_name == "Agent":
        detail = inp.get("description", inp.get("prompt", ""))

    if detail:
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{label}: {detail}"
    return label


def md_to_telegram(text: str) -> str:
    """Convert GitHub-flavored Markdown to Telegram MarkdownV2."""
    # Preserve code blocks first (don't touch content inside them)
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    # Save fenced code blocks
    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    # Save inline code
    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", _save_inline, text)

    # Headings: ## Text → *Text*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Split by our placeholders, escape only non-code parts
    parts = re.split(r"(\x00(?:CODEBLOCK|INLINE)\d+\x00)", text)
    result = []
    for part in parts:
        if part.startswith("\x00CODEBLOCK"):
            idx = int(part.strip("\x00").replace("CODEBLOCK", ""))
            result.append(code_blocks[idx])
        elif part.startswith("\x00INLINE"):
            idx = int(part.strip("\x00").replace("INLINE", ""))
            result.append(inline_codes[idx])
        else:
            # Escape special chars but preserve * for bold/headings
            part = re.sub(r"([_\[\]()~>+\-=|{}.!\\#])", r"\\\1", part)
            result.append(part)

    return "".join(result)


class TelegramStreamer:
    """Accumulates text and pushes it to Telegram via message editing.

    All Telegram I/O runs in a background flusher task so callers
    (the Claude executor) never block on Telegram.  ``append`` and
    ``send_tool_notification`` only mutate local state and wake the
    flusher — if Telegram is rate-limited or unreachable, Claude keeps
    reading its stdout pipe and the flusher catches up later.
    """

    _FINISH_DRAIN_TIMEOUT = 30.0  # bounded wait for final drain
    _ERROR_SEND_TIMEOUT = 5.0

    def __init__(self, gateway: TelegramGateway, chat_id: int | str) -> None:
        self._gateway = gateway
        self._chat_id = chat_id
        self._messages: list[int] = []  # sent message IDs
        self._buffer = ""
        self._last_edit: float = 0.0
        self._current_msg_id: int | None = None
        self._status_msg_id: int | None = None
        self._tool_count: int = 0
        self._status_description: str = ""
        self._status_started: float = 0.0
        self._status_ticker: asyncio.Task | None = None

        # Decoupling state — the flusher owns all gateway interaction
        self._pending_text: str = ""
        self._pending_tool: tuple[str, Any] | None = None
        self._wake = asyncio.Event()
        self._closing = False
        self._flusher: asyncio.Task | None = None

    async def start(self) -> None:
        self._current_msg_id = None
        self._buffer = ""
        self._last_edit = time.monotonic()
        # Typing indicator is cosmetic — don't block on it
        asyncio.create_task(self._safe_chat_action())
        if self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop())

    async def _safe_chat_action(self) -> None:
        try:
            await self._gateway.send_chat_action(self._chat_id, ChatAction.TYPING)
        except Exception:
            pass  # Non-critical — typing indicator is cosmetic

    async def append(self, text: str) -> None:
        """Enqueue text for the flusher — never blocks on Telegram."""
        if not text:
            return
        self._pending_text += redact(text)
        self._wake.set()

    async def send_tool_notification(self, tool_name: str, tool_input: Any) -> None:
        """Enqueue a tool-activity notification for the flusher.

        Only the latest pending notification is kept — if multiple tool
        calls fire before the flusher runs, older ones are collapsed.
        """
        self._pending_tool = (tool_name, tool_input)
        self._wake.set()

    async def finish(self) -> None:
        """Signal the flusher to drain remaining output and exit.

        Idempotent.  Always returns within ``_FINISH_DRAIN_TIMEOUT``
        seconds even if Telegram is unreachable — the flusher is
        cancelled on timeout so the caller never hangs.
        """
        self._closing = True
        self._wake.set()
        flusher = self._flusher
        if flusher is None:
            return
        try:
            await asyncio.wait_for(flusher, timeout=self._FINISH_DRAIN_TIMEOUT)
        except asyncio.TimeoutError:
            flusher.cancel()
            try:
                await flusher
            except (asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Flusher raised during finish", exc_info=True)
        finally:
            self._flusher = None

    async def _flush_loop(self) -> None:
        """Drain pending text/tool updates to Telegram in the background."""
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()

                pending_tool = self._pending_tool
                self._pending_tool = None
                pending_text = self._pending_text
                self._pending_text = ""

                try:
                    if pending_tool is not None:
                        await self._handle_tool_notification(*pending_tool)
                    if pending_text:
                        self._buffer += pending_text
                        await self._render_buffer()
                except Exception:
                    log.warning("Flush iteration failed (non-fatal)", exc_info=True)

                if self._closing and not self._pending_text and self._pending_tool is None:
                    try:
                        await self._final_drain()
                    except Exception:
                        log.warning("Final drain failed", exc_info=True)
                    return

                # Pace edits; new append()s during the sleep just coalesce
                await asyncio.sleep(MIN_EDIT_INTERVAL)
        except asyncio.CancelledError:
            raise

    async def _render_buffer(self) -> None:
        """Send the first message, edit current, or overflow to a new one."""
        if self._current_msg_id is None:
            msg = await self._send_formatted(self._buffer[:4096])
            if msg is None:
                return  # gateway rate-limited — buffer retained, retry next wake
            self._current_msg_id = msg.message_id
            self._messages.append(msg.message_id)
            self._last_edit = time.monotonic()
            return

        if len(self._buffer) > MAX_MSG_LEN:
            await self._finalize_current()
            return

        now = time.monotonic()
        if now - self._last_edit >= MIN_EDIT_INTERVAL:
            await self._edit_current()

    async def _final_drain(self) -> None:
        """Last-chance flush of any remaining buffered content."""
        await self._delete_status()

        if not self._buffer:
            return

        if self._current_msg_id is None:
            msg = await self._send_formatted(self._buffer[:4096])
            if msg is None:
                return
            self._current_msg_id = msg.message_id
            self._messages.append(msg.message_id)
            return

        if len(self._buffer) > MAX_MSG_LEN:
            await self._finalize_current()

        await self._edit_current()

    async def _handle_tool_notification(self, tool_name: str, tool_input: Any) -> None:
        """Finalize current text then update the status message."""
        if self._current_msg_id and self._buffer.strip():
            try:
                await self._edit_current()
            except Exception:
                pass
            self._current_msg_id = None
            self._buffer = ""

        self._tool_count += 1
        self._status_description = redact(_humanize_tool(tool_name, tool_input))
        self._status_started = time.monotonic()

        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()

        await self._update_status_text()

        self._status_ticker = asyncio.create_task(self._run_status_ticker())

    async def _run_status_ticker(self) -> None:
        """Periodically update the status message with elapsed time."""
        try:
            while True:
                await asyncio.sleep(10)
                await self._update_status_text()
        except asyncio.CancelledError:
            pass

    def _format_elapsed(self) -> str:
        elapsed = int(time.monotonic() - self._status_started)
        if elapsed < 60:
            return f"{elapsed}s"
        return f"{elapsed // 60}m {elapsed % 60:02d}s"

    async def _update_status_text(self) -> None:
        elapsed = self._format_elapsed()
        status_text = f"⏳ {self._status_description} ({elapsed})"
        try:
            if self._status_msg_id is None:
                msg = await self._gateway.send_message(
                    self._chat_id, status_text,
                )
                if msg is not None:
                    self._status_msg_id = msg.message_id
            else:
                await self._gateway.edit_message_text(
                    self._chat_id,
                    self._status_msg_id,
                    status_text,
                )
        except BadRequest:
            pass  # Non-critical — skip if update fails

    async def error(self, error_text: str) -> None:
        """Send an error message (fire-and-forget — must never crash or hang).

        Uses a short timeout so a wedged Telegram gateway can't stall the
        caller's error path.
        """
        try:
            await asyncio.wait_for(
                self._gateway.send_message(
                    self._chat_id, f"❌ {redact(error_text)}",
                ),
                timeout=self._ERROR_SEND_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Timed out sending error message to Telegram")
        except Exception:
            log.warning("Failed to send error message to Telegram", exc_info=True)

    # ── Internal ──────────────────────────────────────────────────────

    async def _send_formatted(self, text: str) -> Any:
        """Send a new message with MarkdownV2, falling back to plain text."""
        try:
            return await self._gateway.send_message(
                self._chat_id,
                md_to_telegram(text),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except BadRequest:
            try:
                return await self._gateway.send_message(
                    self._chat_id, text,
                )
            except Exception:
                return None

    async def _delete_status(self) -> None:
        """Cancel the ticker and delete the ephemeral status message."""
        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()
            self._status_ticker = None
        if self._status_msg_id is not None:
            try:
                await self._gateway.delete_message(self._chat_id, self._status_msg_id)
            except Exception:
                pass
            self._status_msg_id = None

    async def _edit_current(self) -> None:
        if not self._current_msg_id or not self._buffer:
            return
        try:
            formatted = md_to_telegram(self._buffer[:4096])
            await self._gateway.edit_message_text(
                self._chat_id,
                self._current_msg_id,
                formatted,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            self._last_edit = time.monotonic()
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                log.warning("Markdown parse failed, falling back to plain text: %s", e)
                try:
                    await self._gateway.edit_message_text(
                        self._chat_id,
                        self._current_msg_id,
                        self._buffer[:4096],
                    )
                    self._last_edit = time.monotonic()
                except Exception:
                    pass

    async def _finalize_current(self) -> None:
        """Finalize the current message at MAX_MSG_LEN and start a new one."""
        finalize_text = self._buffer[:MAX_MSG_LEN]
        overflow = self._buffer[MAX_MSG_LEN:]

        self._buffer = finalize_text
        await self._edit_current()

        # Start new message with overflow
        msg = await self._send_formatted(overflow or "...")
        if msg is None:
            return  # Rate limited — content stays in buffer
        self._current_msg_id = msg.message_id
        self._messages.append(msg.message_id)
        self._buffer = overflow or ""
        self._last_edit = time.monotonic()
