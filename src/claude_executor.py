"""Queue-based worker that invokes Claude Agent SDK and routes output."""

from __future__ import annotations

import asyncio
import logging
import os
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

# Patch _build_command to use --append-system-prompt-file instead of
# --append-system-prompt.  The persona can be >128KB which exceeds the
# effective ARG_MAX in some container environments, causing E7.
import tempfile as _tempfile

_original_build_command = _sdk_client.SubprocessCLITransport._build_command


def _patched_build_command(self: _sdk_client.SubprocessCLITransport) -> list[str]:
    # Temporarily clear append_system_prompt so the original doesn't add it
    saved = self._options.append_system_prompt
    self._options.append_system_prompt = None
    cmd = _original_build_command(self)
    self._options.append_system_prompt = saved

    if saved:
        # Write to a temp file that persists for the subprocess lifetime
        f = _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(saved)
        f.close()
        cmd.extend(["--append-system-prompt-file", f.name])
        # Store ref so we can clean up later (best-effort)
        if not hasattr(self, "_prompt_tempfiles"):
            self._prompt_tempfiles = []
        self._prompt_tempfiles.append(f.name)

    return cmd


_sdk_client.SubprocessCLITransport._build_command = _patched_build_command
from .superpos_client import SuperposClient
from .config import Config
from .module_loader import collect_mcp_servers, discover_modules
from .runtime_config import RuntimeConfig
from .session_store import SessionStore
from .telegram_gateway import TelegramGateway
from .telegram_streamer import TelegramStreamer
from .worktree_manager import ensure_worktree, is_git_repo, worktree_path

log = logging.getLogger(__name__)


@dataclass
class ExecutionRequest:
    prompt: str
    chat_id: int | str
    source: str  # "telegram" | "superpos"
    superpos_task_id: str | None = None
    branch: str | None = None
    image_paths: list[str] | None = None


_modules = discover_modules()
_mcp = collect_mcp_servers(_modules)


class ClaudeExecutor:
    def __init__(
        self,
        config: Config,
        runtime: RuntimeConfig,
        superpos: SuperposClient | None,
        gateway: TelegramGateway | None,
        persona: str | None = None,
    ) -> None:
        self._config = config
        self._runtime = runtime
        self._superpos = superpos
        self._gateway = gateway
        self._persona = persona
        self._persona_version: int | None = None
        self._sessions = SessionStore()
        self.queue: asyncio.Queue[ExecutionRequest] = asyncio.Queue()
        self._in_flight_superpos_tasks: set[str] = set()
        self._semaphore = asyncio.Semaphore(config.claude_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}
        self._active_count: int = 0

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Update the persona used for future executions.

        When ``version`` is provided and is newer than the previously tracked
        version, the next message in each chat will start a fresh session
        instead of resuming. This prevents Claude from inheriting an old
        identity (or other persona-baked context) from conversation history
        written under the previous persona.
        """
        self._persona = prompt
        prev_version = self._persona_version
        if version is not None:
            self._persona_version = version
            if prev_version is not None and version > prev_version:
                log.info(
                    "Persona version bumped %s -> %s; sessions started under "
                    "older persona will be invalidated on next use",
                    prev_version, version,
                )

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
        return len(self._in_flight_superpos_tasks) < self._config.claude_max_parallel

    def add_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.add(task_id)

    def remove_superpos_task(self, task_id: str) -> None:
        self._in_flight_superpos_tasks.discard(task_id)

    def has_superpos_task(self, task_id: str) -> bool:
        return task_id in self._in_flight_superpos_tasks

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
        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning("Claim expired while waiting for semaphore: %s", req.superpos_task_id)
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
                        log.warning("Claim expired while waiting for worktree lock: %s", req.superpos_task_id)
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
            if req.superpos_task_id:
                self.remove_superpos_task(req.superpos_task_id)
            self.queue.task_done()

    async def _report_progress(
        self, task_id: str, claim_expired: asyncio.Event, interval: int = 30
    ) -> None:
        """Send periodic progress updates to keep the Superpos task alive."""
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._superpos.update_progress(task_id, progress)
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
        if self._active_count == 1 and self._superpos:
            try:
                await self._superpos.update_status("busy")
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
            if req.source == "superpos" and req.superpos_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                # Max execution timeout — safety net against zombie pipes
                # where the Claude subprocess dies but grandchild processes
                # keep stdout open, hanging the async-for loop forever.
                max_timeout = self._config.claude_max_turns * 120  # ~2min per turn
                await asyncio.wait_for(inner_task, timeout=max_timeout)
            except asyncio.TimeoutError:
                log.warning(
                    "Execution timed out after %ds for task %s — possible zombie pipe",
                    max_timeout, req.superpos_task_id or req.chat_id,
                )
                inner_task.cancel()
                try:
                    await inner_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    log.warning(
                        "Execution aborted: claim expired for superpos task %s",
                        req.superpos_task_id,
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
            # Always drain/close the streamer — idempotent, bounded by
            # its own timeout so a wedged Telegram gateway can't hang us.
            try:
                await streamer.finish()
            except Exception:
                log.debug("Streamer finish failed (non-fatal)", exc_info=True)
            # Clean up temp media files
            if req.image_paths:
                for p in req.image_paths:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            self._active_count -= 1
            if self._active_count == 0 and self._superpos:
                try:
                    await self._superpos.update_status("online")
                except Exception:
                    log.debug("Failed to set agent status to online")

    async def run_dream(self, task_id: str, prompt: str) -> None:
        """Backwards-compatible alias for dream tasks."""
        await self.run_background(task_id, prompt, task_type="dream")

    async def run_background(
        self,
        task_id: str,
        prompt: str,
        task_type: str = "dream",
        timeout_seconds: int = 300,
    ) -> None:
        """Execute a background task (dream, knowledge_fillin, …).

        No streamer, no semaphore.  The inner ``query`` loop runs inside a
        child task so we can forcibly cancel it when the Superpos claim
        expires or the overall timeout fires — otherwise a silent Claude
        subprocess hangs the reader forever (TASK-stuck-dream scenario).
        """
        label = task_type.replace("_", " ")
        log.info("%s task %s starting in background", label.capitalize(), task_id)

        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None
        if self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(task_id, claim_expired)
            )

        full_text = ""

        async def _run_inner() -> None:
            nonlocal full_text
            options = self._build_options()
            async for message in query(prompt=prompt, options=options):
                text = self._extract_text(message)
                if text:
                    full_text += text

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None and not inner_task.done():
                inner_task.cancel()

        expired = False
        timed_out = False
        try:
            inner_task = asyncio.create_task(_run_inner())
            watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                await asyncio.wait_for(inner_task, timeout=timeout_seconds)
            except asyncio.TimeoutError:
                timed_out = True
                log.warning(
                    "%s task %s timed out after %ds — cancelling",
                    label.capitalize(), task_id, timeout_seconds,
                )
                inner_task.cancel()
                try:
                    await inner_task
                except (asyncio.CancelledError, Exception):
                    pass
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    expired = True
                    log.warning(
                        "%s task %s cancelled: claim expired", label.capitalize(), task_id,
                    )
                else:
                    raise

            if expired:
                return

            if timed_out:
                if self._superpos and not claim_expired.is_set():
                    try:
                        await self._superpos.fail_task(
                            task_id, f"{label.capitalize()} timed out after {timeout_seconds}s",
                        )
                    except Exception:
                        log.debug("Failed to mark timed-out task %s", task_id)
                return

            result = full_text[-2000:] if len(full_text) > 2000 else full_text
            summary = {
                "description": f"{label.capitalize()}: automated background task",
                "output_excerpt": full_text[:500] if full_text else None,
            }
            if self._superpos and not claim_expired.is_set():
                await self._superpos.complete_task(task_id, result, summary=summary)
            log.info("%s task %s completed", label.capitalize(), task_id)
        except Exception:
            log.warning("%s task %s failed", label.capitalize(), task_id, exc_info=True)
            if self._superpos and not claim_expired.is_set():
                try:
                    await self._superpos.fail_task(task_id, f"{label.capitalize()} failed")
                except Exception:
                    pass
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            if progress_task:
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass

    # The Claude SDK passes both the prompt (--print) and system prompt
    # (--append-system-prompt) as CLI arguments.  Linux ARG_MAX is ~2MB,
    # so the combined size must stay well under that.
    _MAX_CLI_BUDGET = 1_500_000  # 1.5MB safe limit

    def _build_options(
        self,
        resume_session: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> ClaudeCodeOptions:
        """Build ClaudeCodeOptions, optionally resuming a session or overriding cwd."""
        opts: dict = {
            "model": self._runtime.model,
            "max_turns": self._config.claude_max_turns,
            "permission_mode": "bypassPermissions",
            "cwd": cwd or self._config.claude_working_dir,
            "extra_args": {"effort": self._runtime.effort},
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
            system_prompt = "\n\n".join(parts)
            if len(system_prompt) > self._MAX_CLI_BUDGET:
                log.warning(
                    "System prompt too large (%dKB), truncating to fit CLI limits",
                    len(system_prompt) // 1024,
                )
                system_prompt = system_prompt[:self._MAX_CLI_BUDGET]
            opts["append_system_prompt"] = system_prompt
        return ClaudeCodeOptions(**opts)

    async def _execute_inner(
        self, req: ExecutionRequest, streamer: TelegramStreamer, retries: int,
    ) -> None:
        t0 = time.monotonic()
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

        # Inject branching instructions for tasks without an explicit branch
        system_prompt_append: str | None = None
        wt_base = self._config.claude_working_dir
        if (
            not req.branch
            and is_git_repo(wt_base)
        ):
            if self._config.claude_worktree_isolation:
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
            else:
                system_prompt_append = (
                    "## Git Branching\n"
                    "When this task requires implementing code changes:\n"
                    f"1. First run `git -C {wt_base} fetch origin` to get latest refs.\n"
                    f"2. ALWAYS create your branch from origin/main:\n"
                    f"   `git -C {wt_base} checkout -b <branch-name> origin/main`\n"
                    "3. Do your work, commit, push, and open a PR.\n"
                    "CRITICAL: NEVER branch from the current HEAD — it may be on an unmerged "
                    "feature branch from a previous task. Always use origin/main as the base.\n"
                    "For conversational replies or read-only tasks, skip this entirely."
                )

        # Telegram messages resume the chat session; Superpos tasks run fresh
        resume_id = None
        if req.source == "telegram":
            stored = self._sessions.get_with_version(req.chat_id)
            if stored is not None:
                resume_id, stored_version = stored
                # Lazy invalidation: if the persona has been updated since this
                # session started, drop the resume. Conversation history under
                # the old persona would otherwise override the new identity /
                # behavior (Claude tends to stay consistent with prior turns).
                if (
                    self._persona_version is not None
                    and stored_version is not None
                    and stored_version < self._persona_version
                ):
                    log.info(
                        "Dropping resume for chat %s: session persona v%s < "
                        "current v%s — starting fresh",
                        req.chat_id, stored_version, self._persona_version,
                    )
                    self._sessions.clear(req.chat_id)
                    resume_id = None

        # Prepend image references so Claude reads them via the Read tool
        prompt_text = req.prompt
        if req.image_paths:
            image_refs = "\n".join(f"- {p}" for p in req.image_paths)
            prompt_text = (
                f"The user sent these images. Read them first, then respond.\n"
                f"{image_refs}\n\n{prompt_text}"
            )

        # Cap prompt size — the SDK passes it as a CLI arg (--print),
        # which combined with --append-system-prompt must fit in ARG_MAX.
        prompt_budget = self._MAX_CLI_BUDGET - len(self._persona or "")
        if len(prompt_text) > prompt_budget:
            log.warning("Prompt too large (%dKB), truncating", len(prompt_text) // 1024)
            prompt_text = prompt_text[:prompt_budget] + "\n... (truncated)"

        for attempt in range(1, retries + 1):
            try:
                options = self._build_options(
                    resume_session=resume_id,
                    cwd=cwd_override,
                    system_prompt_append=system_prompt_append,
                )
                async for message in query(
                    prompt=prompt_text,
                    options=options,
                ):
                    # Capture session_id from result
                    if isinstance(message, ResultMessage) and hasattr(message, "session_id"):
                        sid = message.session_id
                        if sid and req.source == "telegram":
                            self._sessions.set_with_version(
                                req.chat_id, sid, self._persona_version,
                            )

                    text = self._extract_text(message)
                    if text:
                        full_text += text
                        await streamer.append(text)

                    tool_info = self._extract_tool_use(message)
                    if tool_info:
                        await streamer.send_tool_notification(*tool_info)

                await streamer.finish()

                # Complete Superpos task if applicable
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    result = full_text[-2000:] if len(full_text) > 2000 else full_text
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "output_excerpt": full_text[:500] if full_text else None,
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.complete_task(
                            req.superpos_task_id, result, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to complete superpos task %s — claim may have expired",
                            req.superpos_task_id, exc_info=True,
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

                # Transient API errors (500, overloaded) are safe to retry
                # even with output — Claude resumes the session.
                is_api_500 = (
                    "internal server error" in err_str.lower()
                    or "api_error" in err_str.lower()
                    or "overloaded" in err_str.lower()
                )
                if is_api_500 and attempt < retries:
                    wait = 30 * attempt
                    log.warning(
                        "API server error (attempt %d/%d), retrying in %ds: %s",
                        attempt, retries, wait, err_str[:100],
                    )
                    await streamer.append(f"\n⏳ API error, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue

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
                if req.source == "superpos" and req.superpos_task_id and self._superpos:
                    elapsed = int(time.monotonic() - t0)
                    summary = {
                        "description": req.prompt[:200],
                        "error": err_str[:500],
                        "duration_seconds": elapsed,
                    }
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning("Failed to mark superpos task %s as failed", req.superpos_task_id)
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
