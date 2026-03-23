"""Queue-based worker that invokes Claude Agent SDK and routes output."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass

import httpx

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
from .apiary_client import ApiaryClient
from .config import Config
from .module_loader import collect_mcp_servers, discover_modules
from .session_store import SessionStore
from .telegram_gateway import TelegramGateway
from .telegram_streamer import TelegramStreamer
from .worktree_manager import ensure_worktree, is_git_repo, worktree_path

log = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "apiary"
    apiary_task_id: str | None = None
    branch: str | None = None


_modules = discover_modules()
_mcp = collect_mcp_servers(_modules)


class ClaudeExecutor:
    def __init__(
        self,
        config: Config,
        apiary: ApiaryClient | None,
        gateway: TelegramGateway,
        persona: str | None = None,
    ) -> None:
        self._config = config
        self._apiary = apiary
        self._gateway = gateway
        self._persona = persona
        self._sessions = SessionStore()
        self.queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()
        self._in_flight_apiary_tasks: set[str] = set()
        self._semaphore = asyncio.Semaphore(config.claude_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        self._active_count: int = 0

    def update_persona(self, prompt: str | None) -> None:
        """Update the persona used for future executions."""
        self._persona = prompt

    @property
    def pending(self) -> int:
        return self.queue.qsize()

    @property
    def is_busy(self) -> bool:
        """True if any task is currently executing."""
        return self._active_count > 0

    @property
    def has_free_slots(self) -> bool:
        """True if the executor can accept more concurrent tasks.

        Uses the in-flight task set (populated at claim time, cleared after
        execution) to accurately count tasks that are queued, waiting for
        the semaphore, OR actively executing.  ``queue.qsize()`` and
        ``_active_count`` both miss the semaphore-waiting gap.
        """
        return len(self._in_flight_apiary_tasks) < self._config.claude_max_parallel

    def add_apiary_task(self, task_id: str) -> None:
        self._in_flight_apiary_tasks.add(task_id)

    def remove_apiary_task(self, task_id: str) -> None:
        self._in_flight_apiary_tasks.discard(task_id)

    def has_apiary_task(self, task_id: str) -> bool:
        return task_id in self._in_flight_apiary_tasks

    def clear_session(self, chat_id: int | str) -> None:
        """Clear the stored session for a chat, starting fresh next message."""
        self._sessions.clear(chat_id)

    def _get_worktree_lock(self, slot: str) -> asyncio.Lock:
        if slot not in self._worktree_locks:
            self._worktree_locks[slot] = asyncio.Lock()
        return self._worktree_locks[slot]

    def _resolve_slot(self, req: ExecutionRequest) -> str:
        if (
            req.branch
            and self._config.claude_worktree_isolation
            and is_git_repo(self._config.claude_working_dir)
        ):
            return worktree_path(self._config.claude_working_dir, req.branch)
        return "__main__"

    async def run(self) -> None:
        """Infinite loop: pull requests from queue, dispatch concurrent workers."""
        log.info("Claude executor started (max_parallel=%d)", self._config.claude_max_parallel)
        while True:
            req = await self.queue.get()
            asyncio.create_task(self._run_one(req))

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        # Start heartbeat IMMEDIATELY — before semaphore/worktree waits.
        # This keeps the server-side claim alive while queued.
        if req.source == "apiary" and req.apiary_task_id and self._apiary:
            progress_task = asyncio.create_task(
                self._report_progress(req.apiary_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning("Claim expired while waiting for semaphore: %s", req.apiary_task_id)
                    return

                slot = self._resolve_slot(req)
                wt_lock = self._get_worktree_lock(slot)

                # Wait for worktree lock OR claim expiry — whichever comes first
                lock_acquired = False
                try:
                    lock_task = asyncio.create_task(wt_lock.acquire())
                    expire_task = asyncio.create_task(claim_expired.wait())
                    done, pending = await asyncio.wait(
                        [lock_task, expire_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for p in pending:
                        p.cancel()
                        try:
                            await p
                        except asyncio.CancelledError:
                            pass

                    if claim_expired.is_set():
                        # Release lock if we got it while also expiring
                        if lock_task in done and lock_task.result():
                            wt_lock.release()
                        log.warning("Claim expired while waiting for worktree lock: %s", req.apiary_task_id)
                        return

                    lock_acquired = True
                    await self._execute(req, claim_expired)
                finally:
                    if lock_acquired:
                        wt_lock.release()
        except asyncio.CancelledError:
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            log.warning("Spurious CancelledError during execution (suppressed)")
        except Exception:
            log.exception("Execution failed for request: %s", req)
        finally:
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            if req.apiary_task_id:
                self.remove_apiary_task(req.apiary_task_id)
            self.queue.task_done()

    async def _report_progress(
        self, task_id: str, claim_expired: asyncio.Event, interval: int = 30
    ) -> None:
        """Send periodic progress updates to keep the Apiary task alive."""
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._apiary.update_progress(task_id, progress)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    log.warning("Claim expired for task %s (409); aborting execution", task_id)
                    claim_expired.set()
                    return
                log.debug("Progress update failed for task %s", task_id)
            except Exception:
                log.debug("Progress update failed for task %s", task_id)

    async def _execute(
        self, req: ExecutionRequest, claim_expired: asyncio.Event, retries: int = 3,
    ) -> None:
        self._active_count += 1
        if self._active_count == 1 and self._apiary:
            try:
                await self._apiary.update_status("busy")
            except Exception:
                log.debug("Failed to set agent status to busy")

        streamer = TelegramStreamer(self._gateway, req.chat_id)
        try:
            await streamer.start()
        except Exception:
            log.debug("Streamer start failed (non-fatal)")
        t0 = time.monotonic()
        full_text = ""

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None:
                inner_task.cancel()

        try:
            inner_task = asyncio.create_task(self._execute_inner(req, streamer, retries))
            if req.source == "apiary" and req.apiary_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await inner_task
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    log.warning(
                        "Execution aborted: claim expired for apiary task %s",
                        req.apiary_task_id,
                    )
                else:
                    raise
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            self._active_count -= 1
            if self._active_count == 0 and self._apiary:
                try:
                    await self._apiary.update_status("online")
                except Exception:
                    log.debug("Failed to set agent status to online")

    def _build_options(
        self,
        resume_session: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> ClaudeCodeOptions:
        """Build ClaudeCodeOptions, optionally resuming a session or overriding cwd."""
        opts: dict = {
            "model": self._config.claude_model,
            "max_turns": self._config.claude_max_turns,
            "permission_mode": "bypassPermissions",
            "cwd": cwd or self._config.claude_working_dir,
        }
        if _mcp:
            opts["mcp_servers"] = _mcp
        if resume_session:
            opts["resume"] = resume_session
        parts = []
        if self._persona:
            parts.append(self._persona)
        if system_prompt_append:
            parts.append(system_prompt_append)
        if parts:
            opts["append_system_prompt"] = "\n\n".join(parts)
        return ClaudeCodeOptions(**opts)

    async def _execute_inner(
        self, req: ExecutionRequest, streamer: TelegramStreamer, retries: int,
    ) -> None:
        full_text = ""

        # Resolve worktree cwd for tasks that carry an explicit branch
        cwd_override: str | None = None
        if (
            req.branch
            and self._config.claude_worktree_isolation
            and is_git_repo(self._config.claude_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.claude_working_dir, req.branch
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    req.branch,
                    exc_info=True,
                )

        # Inject worktree instructions for requests without an explicit branch
        system_prompt_append: str | None = None
        if (
            not req.branch
            and self._config.claude_worktree_isolation
            and is_git_repo(self._config.claude_working_dir)
        ):
            wt_base = self._config.claude_working_dir
            system_prompt_append = (
                "## Worktree Isolation\n"
                "When this task requires implementing code changes on a new branch:\n"
                f"1. First run `git -C {wt_base} fetch origin` to get latest refs.\n"
                f"2. Choose a branch name, then: `git worktree add {wt_base}/.worktrees/<branch> -b <branch> origin/main`\n"
                f"3. Do all file edits and git operations inside `{wt_base}/.worktrees/<branch>`\n"
                "4. Commit, push the branch, and open a PR from the worktree.\n"
                "IMPORTANT: Always branch from origin/main to avoid inheriting unrelated in-progress work.\n"
                "NEVER create branches from the current HEAD of the main workspace — it may be on an unmerged feature branch.\n"
                "For conversational replies or read-only tasks, skip this entirely."
            )

        # Telegram messages resume the chat session; Apiary tasks run fresh
        resume_id = None
        if req.source == "telegram":
            resume_id = self._sessions.get(req.chat_id)

        for attempt in range(1, retries + 1):
            try:
                options = self._build_options(
                    resume_session=resume_id,
                    cwd=cwd_override,
                    system_prompt_append=system_prompt_append,
                )
                async for message in query(
                    prompt=req.prompt,
                    options=options,
                ):
                    # Capture session_id from result
                    if isinstance(message, ResultMessage) and hasattr(message, "session_id"):
                        sid = message.session_id
                        if sid and req.source == "telegram":
                            self._sessions.set(req.chat_id, sid)

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
                is_oauth_expired = (
                    "OAuth token has expired" in err_str
                    or ("oauth" in err_str.lower() and "expired" in err_str.lower())
                )
                is_auth_error = (
                    is_oauth_expired
                    or "authentication_error" in err_str
                    or "Invalid authentication credentials" in err_str
                )

                if is_auth_error:
                    if is_oauth_expired:
                        log.critical(
                            "Claude OAuth session expired. "
                            "Re-run the OAuth flow (see README step 3) then restart. "
                            "Shutting down."
                        )
                    else:
                        log.critical(
                            "Claude authentication failed — API key invalid or OAuth not configured. "
                            "Shutting down."
                        )
                    sys.exit(1)

                # Don't retry if execution already produced output — side
                # effects (GitHub comments, commits, etc.) cannot be undone.
                if full_text.strip():
                    log.warning(
                        "Execution produced output but failed (attempt %d/%d); "
                        "not retrying to avoid duplicate side effects",
                        attempt, retries,
                    )
                elif is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning("Rate limited (attempt %d/%d), retrying in %ds", attempt, retries, wait)
                    await streamer.append(f"\n⏳ Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue
                # If resume failed (stale session), retry without resume
                elif resume_id and attempt < retries:
                    log.warning("Session resume failed, retrying with fresh session")
                    self._sessions.clear(req.chat_id)
                    resume_id = None
                    continue

                if isinstance(e, ClaudeSDKError):
                    log.error("Claude SDK error: %s", e)
                else:
                    log.exception("Unexpected error during execution")
                try:
                    await streamer.error(f"Error: {e}")
                except asyncio.CancelledError:
                    log.warning("CancelledError while sending error to Telegram (suppressed)")
                except Exception:
                    log.warning("Failed to send error notification", exc_info=True)
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
