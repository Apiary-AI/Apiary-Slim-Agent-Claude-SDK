"""Queue-based worker that invokes Claude Agent SDK and routes output.

This is Claude's concrete :class:`superpos_agent_core.Executor` subclass.  Core
modules (``superpos_poller``, ``telegram_bot``, ``run_agent``) drive every
agent through the abstract Executor surface; the Claude-specific bits live
here: SDK patches, persona-as-system-prompt wiring, session resume, cleanup
of ``~/.claude/projects/`` artifacts.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import shutil
import sys
import tempfile as _tempfile
import time

import anyio
import httpx

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKError, Message, ProcessError, query
from claude_code_sdk._internal import client as _sdk_client
from claude_code_sdk._internal import message_parser
from claude_code_sdk.types import AssistantMessage, ResultMessage, SystemMessage

from superpos_agent_core import (
    ExecutionRequest,
    Executor,
    RecentTasksLog,
    SessionStore,
    SuperposClient,
    TaskSummary,
    TelegramGateway,
    TelegramStreamer,
    collect_mcp_servers,
    discover_modules,
    ensure_worktree,
    is_git_repo,
    worktree_path,
)

from .config import ClaudeConfig
from .runtime_config import ClaudeRuntimeConfig

log = logging.getLogger(__name__)


# ── SDK patches ───────────────────────────────────────────────────────────
#
# These monkey-patches keep the Claude CLI bridge usable in our container:
#   1. parse_message — tolerate unknown event types instead of crashing.
#   2. _build_command — pass append_system_prompt via a file when it would
#      otherwise blow ARG_MAX (persona can exceed 128KB).
#   3. anyio.open_process — capture stderr to a tempfile so a CLI crash
#      gives us a real error message instead of "exit code N (no stderr)".

_original_parse = message_parser.parse_message


def _patched_parse(data: dict) -> Message:
    try:
        return _original_parse(data)
    except Exception:
        return SystemMessage(subtype=data.get("type", "unknown"), data=data)


message_parser.parse_message = _patched_parse
_sdk_client.parse_message = _patched_parse


_original_build_command = _sdk_client.SubprocessCLITransport._build_command


def _patched_build_command(self: _sdk_client.SubprocessCLITransport) -> list[str]:
    saved = self._options.append_system_prompt
    self._options.append_system_prompt = None
    cmd = _original_build_command(self)
    self._options.append_system_prompt = saved

    if saved:
        f = _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        f.write(saved)
        f.close()
        cmd.extend(["--append-system-prompt-file", f.name])
        if not hasattr(self, "_prompt_tempfiles"):
            self._prompt_tempfiles = []
        self._prompt_tempfiles.append(f.name)

    return cmd


_sdk_client.SubprocessCLITransport._build_command = _patched_build_command


_stderr_capture_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "claude_cli_stderr_capture", default=None,
)
_original_open_process = anyio.open_process


async def _patched_open_process(*args, **kwargs):
    capture = _stderr_capture_var.get()
    if capture is None or kwargs.get("stderr") is not None:
        return await _original_open_process(*args, **kwargs)

    f = _tempfile.NamedTemporaryFile(
        mode="wb", prefix="claude-stderr-", suffix=".log", delete=False,
    )
    kwargs["stderr"] = f
    try:
        proc = await _original_open_process(*args, **kwargs)
    except Exception:
        try:
            f.close()
            os.unlink(f.name)
        except OSError:
            pass
        raise
    f.close()
    capture["path"] = f.name
    capture["pid"] = proc.pid
    return proc


anyio.open_process = _patched_open_process


def _read_captured_stderr(path: str | None, max_bytes: int = 4096) -> str:
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return ""
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode("utf-8", errors="replace").strip()
        if size > max_bytes:
            text = "…(truncated)…\n" + text
        return text
    except OSError:
        return ""


def _cleanup_captured_stderr(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Auth-failure help text ────────────────────────────────────────────────

_AUTH_HELP_OAUTH_EXPIRED = """
╔══════════════════════════════════════════════════════════════╗
║       Claude OAuth session expired — cannot start           ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Your OAuth session has fully expired. Re-authenticate:      ║
║                                                              ║
║    docker run -it \\                                         ║
║      -v claude_auth:/home/agent/.claude \\                   ║
║      --entrypoint claude superpos-claude-agent               ║
║                                                              ║
║  Open the printed URL in your browser and log in.            ║
║  Then restart the agent (keep the -v flag).                  ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""

_AUTH_HELP_INVALID_KEY = """
╔══════════════════════════════════════════════════════════════╗
║         Claude authentication failed — cannot start         ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Option 1 — OAuth (Claude Pro/Max subscription):            ║
║                                                              ║
║    docker run -it \\                                         ║
║      -v claude_auth:/home/agent/.claude \\                   ║
║      --entrypoint claude superpos-claude-agent               ║
║                                                              ║
║    Open the printed URL in your browser and log in.          ║
║    Then run the agent with the same -v flag.                 ║
║                                                              ║
║  Option 2 — API key:                                         ║
║                                                              ║
║    Set ANTHROPIC_API_KEY=sk-ant-... in your .env file.       ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""


def _auth_error_message(err: str) -> str | None:
    if "OAuth token has expired" in err or ("oauth" in err.lower() and "expired" in err.lower()):
        return _AUTH_HELP_OAUTH_EXPIRED
    if "authentication_error" in err or "Invalid authentication credentials" in err:
        return _AUTH_HELP_INVALID_KEY
    return None


# ── Executor ──────────────────────────────────────────────────────────────


class ClaudeExecutor(Executor):
    """Concrete executor that drives Anthropic's Claude Agent SDK."""

    # SDK passes prompt + system prompt as CLI args; the combined size must
    # fit under Linux ARG_MAX (~2MB).  Stay well clear of the ceiling.
    _MAX_CLI_BUDGET = 1_500_000  # 1.5MB

    def __init__(
        self,
        config: ClaudeConfig,
        runtime: ClaudeRuntimeConfig,
        superpos: SuperposClient | None,
        gateway: TelegramGateway | None,
        persona: str | None = None,
    ) -> None:
        super().__init__(max_parallel=config.executor_max_parallel)
        self._config = config
        self._runtime = runtime
        self._superpos = superpos
        self._gateway = gateway
        self._persona = persona
        self._persona_version: int | None = None
        self._sessions = SessionStore(
            path=os.path.join(config.home_dir, "session_store.json"),
        )
        self._recent_tasks = RecentTasksLog(max_per_chat=5)
        self._semaphore = asyncio.Semaphore(config.executor_max_parallel)
        self._worktree_locks: dict[str, asyncio.Lock] = {}

        modules = discover_modules(config.modules_dir)
        self._mcp = collect_mcp_servers(modules)
        if self._mcp:
            log.info("Loaded %d MCP server(s) from %s", len(self._mcp), config.modules_dir)

    # ── Abstract method impls ────────────────────────────────────────────

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Replace the persona used for subsequent executions.

        When ``version`` is provided and is newer than the previously
        tracked version, the next message in each chat will start a fresh
        session instead of resuming.  Otherwise the LLM inherits its
        previous identity / context from conversation history written
        under the old persona — Claude tends to stay consistent with
        prior turns, so a new ``--append-system-prompt`` alone can't
        overcome an old self-introduction in the resume transcript.
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

    def clear_session(self, chat_id: int | str) -> None:
        self._sessions.clear(chat_id)

    async def run(self) -> None:
        log.info(
            "Claude executor started (max_parallel=%d)",
            self._config.executor_max_parallel,
        )
        while True:
            req = await self.queue.get()
            asyncio.create_task(self._run_one(req))

    # ── Optional hooks ───────────────────────────────────────────────────

    async def preflight(self) -> None:
        """Verify Claude credentials by making a minimal SDK call.

        Core's ``run_agent`` marks the agent ``online`` in Superpos *before*
        invoking preflight (see superpos_agent_core.main.run_agent).  If we exit
        here on bad Claude credentials, the asyncio.gather() finally that
        flips status back to ``offline`` never runs, and Superpos keeps
        advertising us as online until the heartbeat timeout fires.  Flip
        offline ourselves on any preflight failure as a best-effort guard.
        """
        log.info("Verifying Claude authentication...")
        try:
            async for _ in query(
                prompt="hi",
                options=ClaudeCodeOptions(max_turns=1, permission_mode="default"),
            ):
                pass  # consume all messages — breaking early corrupts anyio cancel scopes
            log.info("Claude authentication OK")
        except (ClaudeSDKError, Exception) as e:
            await self._mark_offline_best_effort()
            msg = _auth_error_message(str(e))
            if msg:
                print(msg, file=sys.stderr)
                sys.exit(1)
            else:
                raise

    async def _mark_offline_best_effort(self) -> None:
        """Flip Superpos status to ``offline`` ignoring any errors."""
        if not self._superpos:
            return
        try:
            await self._superpos.update_status("offline")
            log.info("Agent status set to offline (preflight failure)")
        except Exception:
            log.debug(
                "Failed to flip status offline during preflight cleanup",
                exc_info=True,
            )

    def cleanup_stale_sessions(self, max_age_hours: int = 24) -> dict[str, int]:
        """Remove old Claude session data while preserving active resumes.

        ``SessionStore`` persists chat_id → session_id, and the executor
        passes that id as ``--resume`` on the next Telegram message.  If a
        chat has been idle for longer than ``max_age_hours`` its session
        directory under ``~/.claude/projects/`` would otherwise be deleted,
        the next ``--resume`` would silently fall through to a fresh
        session, and the user would experience "agent lost the
        conversation on restart".  We preserve every id still mapped.
        """
        counts = {"projects": 0, "session_env": 0, "bytes_freed": 0}
        cutoff = time.time() - (max_age_hours * 3600)
        preserve: set[str] = self._sessions.active_session_ids()

        claude_dir = os.path.join(os.environ.get("HOME", "/tmp"), ".claude")

        def _session_id_from_name(name: str) -> str:
            # Both `<sid>` (dir) and `<sid>.jsonl` (transcript) live in projects/.
            return name[:-6] if name.endswith(".jsonl") else name

        projects_dir = os.path.join(claude_dir, "projects", "-workspace")
        if os.path.isdir(projects_dir):
            for name in os.listdir(projects_dir):
                path = os.path.join(projects_dir, name)
                if not os.path.isdir(path):
                    continue
                if _session_id_from_name(name) in preserve:
                    continue
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        size = sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fns in os.walk(path)
                            for f in fns
                        )
                        shutil.rmtree(path)
                        counts["projects"] += 1
                        counts["bytes_freed"] += size
                except OSError:
                    pass

        # Claude CLI writes per-session env snapshots to `session-env`
        # (hyphenated).  The pre-port code scanned `session_env` (underscore)
        # which never matched the real directory — so those snapshots have
        # been accumulating untouched in every long-running deployment.
        # The `session_env` key in the returned `counts` dict is unchanged;
        # it's the contract with core's startup-cleanup log line.
        session_env_dir = os.path.join(claude_dir, "session-env")
        if os.path.isdir(session_env_dir):
            for name in os.listdir(session_env_dir):
                path = os.path.join(session_env_dir, name)
                if not os.path.isdir(path):
                    continue
                if _session_id_from_name(name) in preserve:
                    continue
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        size = sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fns in os.walk(path)
                            for f in fns
                        )
                        shutil.rmtree(path)
                        counts["session_env"] += 1
                        counts["bytes_freed"] += size
                except OSError:
                    pass

        if preserve:
            log.info(
                "cleanup_stale_sessions: preserved %d active session(s)",
                len(preserve),
            )
        return counts

    # ── Worktree slot management ─────────────────────────────────────────

    def _get_worktree_lock(self, slot: str) -> asyncio.Lock:
        if slot not in self._worktree_locks:
            self._worktree_locks[slot] = asyncio.Lock()
        return self._worktree_locks[slot]

    def _resolve_slot(self, req: ExecutionRequest) -> str:
        if (
            req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            return worktree_path(self._config.executor_working_dir, req.branch)
        return "__main__"

    # ── Main consumer loop ───────────────────────────────────────────────

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                self._report_progress(req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning(
                        "Claim expired while waiting for semaphore: %s",
                        req.superpos_task_id,
                    )
                    return

                slot = self._resolve_slot(req)
                wt_lock = self._get_worktree_lock(slot)

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
                        if lock_task in done and lock_task.result():
                            wt_lock.release()
                        log.warning(
                            "Claim expired while waiting for worktree lock: %s",
                            req.superpos_task_id,
                        )
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
        self, task_id: str, claim_expired: asyncio.Event, interval: int = 30,
    ) -> None:
        progress = 5
        while True:
            await asyncio.sleep(interval)
            progress = min(progress + 5, 95)
            try:
                await self._superpos.update_progress(task_id, progress)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 409:
                    log.warning(
                        "Claim expired for task %s (409); aborting execution", task_id,
                    )
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

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None:
                inner_task.cancel()

        try:
            inner_task = asyncio.create_task(self._execute_inner(req, streamer, retries))
            # Register with the base class so the /stop Telegram command can
            # find and cancel this in-flight work via cancel_chat(chat_id).
            # Auto-untracks via done callback — no cleanup needed in finally.
            self._track_chat_task(req.chat_id, inner_task)
            if req.source == "superpos" and req.superpos_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            try:
                # Safety net against zombie pipes where the Claude subprocess
                # dies but grandchildren keep stdout open, hanging the
                # async-for loop forever.
                max_timeout = self._config.executor_max_turns * 120  # ~2min/turn
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
            try:
                await streamer.finish()
            except Exception:
                log.debug("Streamer finish failed (non-fatal)", exc_info=True)
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

    # ── Background tasks ─────────────────────────────────────────────────

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
        subprocess hangs the reader forever.
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
                        "%s task %s cancelled: claim expired",
                        label.capitalize(), task_id,
                    )
                else:
                    raise

            if expired:
                return

            if timed_out:
                if self._superpos and not claim_expired.is_set():
                    try:
                        await self._superpos.fail_task(
                            task_id,
                            f"{label.capitalize()} timed out after {timeout_seconds}s",
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

    # ── Options construction & inner execute ─────────────────────────────

    def _build_options(
        self,
        resume_session: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
    ) -> ClaudeCodeOptions:
        opts: dict = {
            "model": self._runtime.model,
            "max_turns": self._config.executor_max_turns,
            "permission_mode": "bypassPermissions",
            "cwd": cwd or self._config.executor_working_dir,
            "extra_args": {"effort": self._runtime.effort},
        }
        if self._mcp:
            opts["mcp_servers"] = self._mcp
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
                system_prompt = system_prompt[: self._MAX_CLI_BUDGET]
            opts["append_system_prompt"] = system_prompt
        return ClaudeCodeOptions(**opts)

    async def _execute_inner(
        self, req: ExecutionRequest, streamer: TelegramStreamer, retries: int,
    ) -> None:
        t0 = time.monotonic()
        full_text = ""

        cwd_override: str | None = None
        if (
            req.branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.executor_working_dir, req.branch,
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    req.branch, exc_info=True,
                )

        system_prompt_append: str | None = None
        wt_base = self._config.executor_working_dir
        if not req.branch and is_git_repo(wt_base):
            if self._config.executor_worktree_isolation:
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

        resume_id = None
        if req.source == "telegram":
            stored = self._sessions.get_with_version(req.chat_id)
            if stored is not None:
                resume_id, stored_version = stored
                # Lazy persona invalidation: if the persona has been updated
                # since this session started, drop the resume.  Conversation
                # history under the old persona would otherwise override the
                # new identity/behavior — Claude stays consistent with prior
                # turns, so the new --append-system-prompt alone can't undo
                # an old self-introduction in the resume transcript.
                #
                # `stored_version is None` covers the startup race where
                # Telegram polling can write a session before the Superpos
                # version poller has populated `_persona_version`.  Without
                # this, those sessions would be permanently exempt from
                # invalidation on later persona bumps.
                if self._persona_version is not None and (
                    stored_version is None
                    or stored_version < self._persona_version
                ):
                    log.info(
                        "Dropping resume for chat %s: session persona v%s < "
                        "current v%s — starting fresh",
                        req.chat_id,
                        "?" if stored_version is None else stored_version,
                        self._persona_version,
                    )
                    self._sessions.clear(req.chat_id)
                    resume_id = None
            recent = self._recent_tasks.render(req.chat_id)
            if recent:
                system_prompt_append = (
                    f"{system_prompt_append}\n\n{recent}"
                    if system_prompt_append else recent
                )

        prompt_text = req.prompt
        if req.image_paths:
            image_refs = "\n".join(f"- {p}" for p in req.image_paths)
            prompt_text = (
                f"The user sent these images. Read them first, then respond.\n"
                f"{image_refs}\n\n{prompt_text}"
            )

        prompt_budget = self._MAX_CLI_BUDGET - len(self._persona or "")
        if len(prompt_text) > prompt_budget:
            log.warning("Prompt too large (%dKB), truncating", len(prompt_text) // 1024)
            prompt_text = prompt_text[:prompt_budget] + "\n... (truncated)"

        # Track the most recent session_id across attempts so a CLI crash
        # mid-task can be resumed (avoids duplicate side effects from retry).
        last_session_id: str | None = None
        effective_prompt = prompt_text

        for attempt in range(1, retries + 1):
            capture: dict = {}
            capture_token = _stderr_capture_var.set(capture)
            try:
                options = self._build_options(
                    resume_session=resume_id,
                    cwd=cwd_override,
                    system_prompt_append=system_prompt_append,
                )
                async for message in query(prompt=effective_prompt, options=options):
                    # Capture session_id from init (fires on every run) and
                    # result (terminal, success only).  Init survives a CLI
                    # crash mid-stream — ResultMessage doesn't fire if the
                    # subprocess dies before completing, so relying on it
                    # alone leaves crash-retry without a session to resume.
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        sid = message.data.get("session_id")
                        if sid:
                            last_session_id = sid
                            if req.source == "telegram":
                                self._sessions.set_with_version(
                                    req.chat_id, sid, self._persona_version,
                                )
                    elif isinstance(message, ResultMessage) and hasattr(message, "session_id"):
                        sid = message.session_id
                        if sid:
                            last_session_id = sid
                            if req.source == "telegram":
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
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="succeeded",
                            detail=full_text[:500] if full_text else "",
                        ),
                    )
                return

            except (ClaudeSDKError, Exception) as e:
                err_str = str(e)
                captured_stderr = _read_captured_stderr(capture.get("path"))
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
                            "Re-run the OAuth flow then restart. Shutting down."
                        )
                    else:
                        log.critical(
                            "Claude authentication failed — API key invalid or OAuth not configured. "
                            "Shutting down."
                        )
                    sys.exit(1)

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

                # CLI subprocess crash — resume the session so completed tool
                # calls aren't re-run.
                is_cli_crash = (
                    isinstance(e, ProcessError)
                    or "Command failed with exit code" in err_str
                )
                if is_cli_crash and last_session_id and attempt < retries:
                    wait = 5 * attempt
                    exit_code = getattr(e, "exit_code", "?")
                    log.warning(
                        "Claude CLI crashed (exit %s, attempt %d/%d); "
                        "resuming session %s in %ds. CLI stderr:\n%s",
                        exit_code, attempt, retries, last_session_id, wait,
                        captured_stderr or "(no stderr captured)",
                    )
                    stderr_blurb = (
                        f"\nCLI stderr tail:\n{captured_stderr}"
                        if captured_stderr else ""
                    )
                    await streamer.append(
                        f"\n⏳ CLI crashed (exit {exit_code}), "
                        f"resuming session in {wait}s...{stderr_blurb}\n",
                    )
                    await asyncio.sleep(wait)
                    resume_id = last_session_id
                    effective_prompt = (
                        "Your previous CLI invocation crashed before completing this task. "
                        "Review your prior outputs in this session to see what was already done, "
                        "then continue from where you left off and finish the task."
                    )
                    continue

                if full_text.strip():
                    log.warning(
                        "Execution produced output but failed (attempt %d/%d); "
                        "not retrying to avoid duplicate side effects",
                        attempt, retries,
                    )
                elif is_rate_limit and attempt < retries:
                    wait = 30 * attempt
                    log.warning(
                        "Rate limited (attempt %d/%d), retrying in %ds",
                        attempt, retries, wait,
                    )
                    await streamer.append(f"\n⏳ Rate limited, retrying in {wait}s...\n")
                    await asyncio.sleep(wait)
                    continue
                elif resume_id and attempt < retries:
                    log.warning("Session resume failed, retrying with fresh session")
                    self._sessions.clear(req.chat_id)
                    resume_id = None
                    continue

                if isinstance(e, ClaudeSDKError):
                    log.error("Claude SDK error: %s", e)
                else:
                    log.exception("Unexpected error during execution")
                hint = ""
                if (
                    isinstance(e, ProcessError)
                    or "Command failed with exit code" in err_str
                ):
                    if captured_stderr:
                        hint = (
                            f"\n💡 The Claude CLI subprocess crashed. "
                            f"Captured stderr (tail):\n{captured_stderr}"
                        )
                    else:
                        hint = (
                            "\n💡 The Claude CLI subprocess crashed but wrote "
                            "nothing to stderr (likely OOM-killed or signaled). "
                            "Check `docker logs <agent>` and `dmesg` near this "
                            "timestamp."
                        )
                try:
                    await streamer.error(f"Error: {e}{hint}")
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
                    if captured_stderr:
                        summary["cli_stderr"] = captured_stderr[-2000:]
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id, err_str, summary=summary,
                        )
                    except Exception:
                        log.warning(
                            "Failed to mark superpos task %s as failed",
                            req.superpos_task_id,
                        )
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=err_str[:500],
                        ),
                    )
                return

            finally:
                # CancelledError (BaseException) bypasses the except clause
                # above, so without finally the delete=False tempfiles leak —
                # claim-expiry/timeout cancellation would accumulate orphans.
                try:
                    _stderr_capture_var.reset(capture_token)
                except (ValueError, LookupError):
                    pass
                _cleanup_captured_stderr(capture.get("path"))

    # ── Message parsing ──────────────────────────────────────────────────

    @staticmethod
    def _extract_text(message: Message) -> str:
        """Extract assistant text from a Claude SDK message.

        Only extract from AssistantMessage — ResultMessage contains a
        duplicate of the already-streamed text.
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
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "name") and hasattr(block, "input"):
                    return (block.name, block.input)
        return None
