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
import json
import logging
import os
import re
import shutil
import sys
import tempfile as _tempfile
import time

import anyio
from claude_code_sdk import (
    ClaudeCodeOptions,
    CLIConnectionError,
    ClaudeSDKError,
    Message,
    ProcessError,
    create_sdk_mcp_server,
    query,
    tool,
)
from claude_code_sdk._internal import client as _sdk_client
from claude_code_sdk._internal import message_parser
from claude_code_sdk.types import AssistantMessage, ResultMessage, SystemMessage

from superpos_agent_core import (
    AskAlreadyPending,
    ExecutionRequest,
    Executor,
    RecentTasksLog,
    SessionStore,
    SuperposClient,
    TaskSummary,
    TelegramGateway,
    TelegramStreamer,
    ask_user_question,
    collect_mcp_servers,
    discover_modules,
    ensure_worktree,
    is_git_repo,
    report_progress,
    worktree_path,
)

from .config import ClaudeConfig
from .runtime_config import ClaudeRuntimeConfig

log = logging.getLogger(__name__)


class PersonaRefreshFailed(RuntimeError):
    """Raised by ``update_persona`` when the assembled-prompt fetch
    failed but the server still reports a real persona version.

    ``superpos_agent_core.superpos_client.get_persona_assembled`` swallows
    both 404s and transport errors and returns ``None`` in either case, so
    the executor cannot tell those apart on its own.  We use the
    accompanying ``version`` argument as the disambiguator: when the
    server-reported ``version`` is not ``None`` but the assembled prompt
    came back as ``None``, that combination can only mean a real fetch
    failure (a server that reports a live version cannot simultaneously
    have no configured persona), so we raise.  When ``version`` is also
    ``None`` we treat the empty response as the valid "no persona
    configured" steady state and clear without raising — matching
    ``superpos_agent_core.main.run_agent``'s startup semantics.

    Raising here is load-bearing: the core poller wraps its persona
    refresh in a broad ``except Exception``, so this exception aborts the
    follow-up ``persona_version = server_version`` assignment, the next
    poll cycle still sees ``changed == True``, and the fetch is retried
    instead of leaving Claude running indefinitely with no persona at all.
    """


# Per-chat SessionStore writes happen at most twice per execution (init +
# result), so 5 iterations gives ample headroom over the realistic worst
# case while still bailing on a runaway loop.
_MAX_SLOT_RESOLVE_ATTEMPTS = 5


# ── SDK patches ───────────────────────────────────────────────────────────
#
# These monkey-patches keep the Claude CLI bridge usable in our container:
#   1. parse_message — tolerate unknown event types instead of crashing.
#   2. _build_command — pass append_system_prompt via a file when it would
#      otherwise blow ARG_MAX (persona can exceed 128KB).
#   3. anyio.open_process — capture stderr to a tempfile so a CLI crash
#      gives us a real error message instead of "exit code N (no stderr)".
#      This only yields anything because _build_options passes the CLI
#      ``--debug-to-stderr``; without that flag the CLI streams everything
#      through the stdout JSON protocol and exits silently on stderr, so a
#      crash logged "(no stderr captured)" and the real reason was lost.

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


class _AskContext:
    """Per-run handle the AskUserQuestion MCP tool closes over.

    The MCP server is built once in ``__init__`` and shared across every
    concurrent run, so the tool handler can't capture a single request.
    Instead each run publishes its own ``_AskContext`` on a
    :data:`_ask_context_var` ``ContextVar`` (the SDK invokes the in-process
    tool handler on the same task that drives ``query``, so the contextvar
    propagates).  The handler reads ``chat_id``/``thread_id`` from it and uses
    the ``pause``/``resume`` callbacks to suspend the stall watchdog (and bump
    the claim heartbeat) for the duration of a parked question.
    """

    def __init__(
        self,
        chat_id: int | str,
        thread_id: int | None,
        pause,
        resume,
    ) -> None:
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.pause = pause
        self.resume = resume


_ask_context_var: contextvars.ContextVar[_AskContext | None] = contextvars.ContextVar(
    "claude_ask_context", default=None,
)


# Name of the SDK MCP server + tool that shadows the native AskUserQuestion.
# The CLI exposes in-process SDK tools as ``mcp__<server>__<tool>``.
_ASK_MCP_SERVER = "superpos_ask"
_ASK_MCP_TOOL = "ask_user"
_ASK_MCP_TOOL_FQN = f"mcp__{_ASK_MCP_SERVER}__{_ASK_MCP_TOOL}"

# MCP servers that are *optional* — nice-to-have tooling (the interactive ask
# server and the shim web-search replacement) rather than load-bearing config.
# When the CLI crashes before init with these present, the degrade safeguard in
# ``_execute_inner`` strips them and retries without ask/search rather than
# looping the identical failing config forever.
_OPTIONAL_MCP_SERVERS = frozenset({_ASK_MCP_SERVER, "minimax", "web_search"})

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


# The crash cause hides in the CLI's --debug-to-stderr firehose, which is
# ~99% per-turn bookkeeping (LSP diagnostics, hook-registry checks, tool
# dispatch).  A raw byte-tail just shows that bookkeeping; these are the
# lines that actually explain an exit-1 — API calls, the streaming
# lifecycle, errors, connection teardown.
_STDERR_SIGNAL_RE = re.compile(
    r"\[API|Stream |first byte|Cleared connection cache|"
    r"connection closed|closed after|"
    # MCP startup is the #34 crash site: keep any MCP/server/connection line so
    # the fatal line right after `MCP server "minimax": Starting connection…`
    # is captured instead of scrolling out of the tail window.
    r"mcp|server|connection|"
    r"error|fail|fatal|exception|abort|disconnect|timeout|reset|"
    r"overload|rate.?limit|unauthorized|forbidden|"
    r"\b(?:401|403|429|500|502|503|529)\b|"
    r"ECONN|ETIMEDOUT|EPIPE|socket hang up",
    re.IGNORECASE,
)

# How much of the file's end to scan.  The crash sequence lives at the very
# end (earlier turns in the same task succeeded), and the file can be many
# MB, so cap the read rather than slurping it all.
_STDERR_READ_WINDOW = 256 * 1024
# Always keep this many trailing raw lines — the exit/teardown context a
# human wants even when it doesn't match the signal filter.
_STDERR_EXIT_TAIL_LINES = 6

# ``BaseExceptionGroup`` is a builtin only on Py3.11+.  On Py3.10 AnyIO still
# raises group-wrapped failures during TaskGroup teardown, but it uses the
# ``exceptiongroup`` backport rather than a builtin — so fall back to that so
# the Py3.10 path recognizes groups too instead of treating them as opaque
# leaves.
try:
    _BASE_EXCEPTION_GROUP: type[BaseException] | None = BaseExceptionGroup
except NameError:  # pragma: no cover - Py3.10 fallback
    try:
        from exceptiongroup import BaseExceptionGroup as _BASE_EXCEPTION_GROUP  # py3.10 backport
    except ImportError:  # pragma: no cover - backport not installed
        _BASE_EXCEPTION_GROUP = None


def _flatten_exception(e: BaseException) -> list[BaseException]:
    """Return all leaf exceptions, recursing into ``(Base)ExceptionGroup``.

    The SDK frequently surfaces a CLI subprocess crash wrapped in a
    ``BaseExceptionGroup`` from the anyio TaskGroup teardown (``query.close()``),
    whose ``str()`` is just ``"unhandled errors in a TaskGroup (...)"``.  The
    real signal (``CLIConnectionError``/``ProcessError`` and exit-code text)
    lives in the nested sub-exceptions, so classification must inspect the
    leaves rather than the top-level wrapper.
    """
    # ``BaseExceptionGroup`` is the Py3.11 builtin (``ExceptionGroup`` is its
    # narrower subclass); both expose ``.exceptions``.  On Py3.10
    # ``_BASE_EXCEPTION_GROUP`` is the ``exceptiongroup`` backport class that
    # AnyIO actually raises.  It is only ``None`` if the backport is missing,
    # in which case every exception is simply its own leaf.
    is_group = (
        _BASE_EXCEPTION_GROUP is not None and isinstance(e, _BASE_EXCEPTION_GROUP)
    )
    if is_group:
        leaves: list[BaseException] = []
        for sub in e.exceptions:  # type: ignore[attr-defined]
            leaves.extend(_flatten_exception(sub))
        return leaves
    return [e]


def _read_captured_stderr(path: str | None, max_bytes: int = 16384) -> str:
    """Return the diagnostically useful tail of the CLI's captured stderr.

    With ``--debug-to-stderr`` the CLI emits a verbose firehose, so a plain
    byte-tail is mostly per-turn noise and the actual exit-1 cause scrolls
    out of the window.  Filter to signal lines (API/stream/error/teardown)
    and always append the final raw lines so the exit context survives.
    Falls back to a raw tail when nothing matches the filter, so we never
    lose information.
    """
    if not path:
        return ""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return ""
        with open(path, "rb") as f:
            if size > _STDERR_READ_WINDOW:
                f.seek(size - _STDERR_READ_WINDOW)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
    except OSError:
        return ""

    lines = text.splitlines()
    signal = [ln for ln in lines if _STDERR_SIGNAL_RE.search(ln)]
    exit_tail = lines[-_STDERR_EXIT_TAIL_LINES:]

    if signal:
        # Signal lines (in order) followed by the exit tail, skipping any
        # tail line already shown so the teardown isn't duplicated.
        body = signal + [ln for ln in exit_tail if ln not in signal]
        rendered = "…(filtered to signal lines; full debug elided)…\n" + "\n".join(body)
    else:
        rendered = "\n".join(exit_tail)
        if size > _STDERR_READ_WINDOW or len(lines) > _STDERR_EXIT_TAIL_LINES:
            rendered = "…(truncated)…\n" + rendered

    rendered = rendered.strip()
    if len(rendered) > max_bytes:
        rendered = "…(truncated)…\n" + rendered[-max_bytes:]
    return rendered


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

        # In-process SDK MCP tool shadowing the native AskUserQuestion: it
        # renders an inline keyboard in Telegram (via agent-core's
        # ask_user_question) and returns the user's pick as the tool result so
        # the run resumes.  Always registered; the native tool is steered off
        # via disallowed_tools in _build_options.
        self._mcp = {**self._mcp, _ASK_MCP_SERVER: self._build_ask_mcp_server()}

        # On an Anthropic-compatible *shim* (e.g. MiniMax or Kimi via
        # ANTHROPIC_BASE_URL), Anthropic's hosted WebSearch/WebFetch server
        # tools don't exist on the other end and fail with HTTP 400.
        # _build_options disallows the dead hosted tools on shim backends;
        # here we wire a replacement web-search MCP when one is configured.
        self._shim_backend = not config.is_native_anthropic
        self._search_note: str | None = None
        if self._shim_backend:
            self._wire_shim_search(config)

    def _build_ask_mcp_server(self):
        """Build the in-process SDK MCP server exposing ``ask_user``.

        The tool's input schema mirrors the native ``AskUserQuestion`` tool: a
        ``questions`` array, each entry carrying ``question``/``header``, an
        ``options`` array of ``{label, description, preview?}``, and an
        optional ``multiSelect`` flag.  The handler closes over the executor
        (for ``self._gateway`` and the configured timeout) and reads the live
        run's ``chat_id``/``thread_id`` from the :data:`_ask_context_var`
        contextvar published by ``_execute_inner``, then delegates to
        agent-core's :func:`ask_user_question`, which renders the inline
        keyboard and parks on the user's reply.
        """
        ask_timeout = float(self._config.claude_ask_timeout)

        input_schema = {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "header": {"type": "string"},
                            "multiSelect": {"type": "boolean"},
                            "options": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "description": {"type": "string"},
                                        "preview": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            },
                        },
                        "required": ["question", "options"],
                    },
                }
            },
            "required": ["questions"],
        }

        @tool(
            _ASK_MCP_TOOL,
            "Ask the user one or more multiple-choice questions and wait for "
            "their answer. Renders interactive buttons in the chat. Use this "
            "whenever you need the user to pick between options or confirm a "
            "direction before continuing. Each question has a short `header`, "
            "the `question` text, and 1-4 `options` (each with a `label` and "
            "an optional `description`/`preview`). Set `multiSelect: true` to "
            "let the user pick several. Returns the selected label(s) per "
            "question; if the user does not respond in time the result is "
            "marked `timed_out` so you can proceed without them.",
            input_schema,
        )
        async def ask_user(args: dict) -> dict:
            ctx = _ask_context_var.get()
            if ctx is None:
                # No live run context — shouldn't happen during a real query;
                # fail closed rather than send to an unknown chat.  Raise so the
                # error survives the SDK MCP adapter: create_sdk_mcp_server only
                # forwards ``content`` and drops a returned ``is_error`` flag,
                # whereas the mcp low-level server wraps a raised exception into
                # a CallToolResult(isError=True) with the message preserved.
                raise RuntimeError(
                    "ask_user is unavailable: no active conversation context."
                )

            questions = args.get("questions") or []
            # Pause the stall watchdog while a question is parked.  pause/resume
            # are reference-counted (see _execute), so concurrent ask calls
            # nest safely: the watchdog only un-pauses once every parked
            # question has resolved.  A call that hits AskAlreadyPending still
            # balances its own pause/resume here, and because the owning call's
            # pause is still outstanding the count never reaches zero while a
            # valid question is pending.
            ctx.pause()
            try:
                result = await ask_user_question(
                    ctx.chat_id,
                    ctx.thread_id,
                    questions,
                    gateway=self._gateway,
                    timeout=ask_timeout,
                )
            except AskAlreadyPending as exc:
                # Re-raise as a tool error: the SDK adapter drops a returned
                # ``is_error`` flag, so only a raised exception reaches Claude
                # as CallToolResult(isError=True).  The ``finally`` below still
                # balances the pause/resume; it does not suppress the raise.
                raise RuntimeError(
                    "A question is already pending in this chat. Wait for the "
                    "user to answer it before asking another."
                ) from exc
            finally:
                ctx.resume()

            return {
                "content": [
                    {"type": "text", "text": json.dumps(result)}
                ]
            }

        return create_sdk_mcp_server(
            name=_ASK_MCP_SERVER,
            version="1.0.0",
            tools=[ask_user],
        )

    def _wire_shim_search(self, config: ClaudeConfig) -> None:
        """Pick the replacement web-search MCP for a non-Anthropic backend.

        Precedence: an explicit WEB_SEARCH_MCP server config wins; otherwise
        a MINIMAX_API_KEY implies MiniMax's own web-search MCP; otherwise the
        agent runs without web access (the dead hosted tools are disallowed
        in _build_options regardless).
        """
        if config.web_search_mcp:
            try:
                server = json.loads(config.web_search_mcp)
            except json.JSONDecodeError as exc:
                log.error(
                    "WEB_SEARCH_MCP is not valid JSON (%s) — web search "
                    "unavailable",
                    exc,
                )
                return
            # Accept either a local stdio server (has "command", e.g. Tavily
            # via npx) or a remote HTTP/SSE server (has "url", e.g. Z.ai's own
            # web_search_prime MCP). Default "type" from whichever is present.
            if not isinstance(server, dict) or not (
                server.get("command") or server.get("url")
            ):
                log.error(
                    "WEB_SEARCH_MCP must be a JSON object with a 'command' "
                    "(stdio) or 'url' (http/sse) key — web search unavailable"
                )
                return
            server.setdefault("type", "http" if server.get("url") else "stdio")
            self._mcp = {**self._mcp, "web_search": server}
            self._search_note = (
                "## Web search\n"
                "The built-in WebSearch/WebFetch tools are unavailable on "
                "this backend. For any web lookup use the tools provided by "
                "the `web_search` MCP server instead."
            )
            log.info(
                "Shim backend (base_url=%s) — added custom web-search MCP "
                "(transport=%s)",
                config.anthropic_base_url,
                server["type"],
            )
        elif config.minimax_api_key:
            self._mcp = {
                **self._mcp,
                "minimax": {
                    "type": "stdio",
                    "command": "uvx",
                    "args": ["minimax-coding-plan-mcp", "-y"],
                    "env": {
                        "MINIMAX_API_KEY": config.minimax_api_key,
                        "MINIMAX_API_HOST": config.minimax_api_host,
                    },
                },
            }
            self._search_note = (
                "## Web search\n"
                "The built-in WebSearch/WebFetch tools are unavailable on "
                "this backend. For any web lookup use the `web_search` MCP "
                "tool (server: minimax) instead."
            )
            log.info(
                "Shim backend (base_url=%s) — added MiniMax web_search MCP",
                config.anthropic_base_url,
            )
        else:
            log.warning(
                "Shim backend (base_url=%s) but neither WEB_SEARCH_MCP nor "
                "MINIMAX_API_KEY is set — web search unavailable (hosted "
                "WebSearch/WebFetch will be disabled)",
                config.anthropic_base_url,
            )

    # ── Abstract method impls ────────────────────────────────────────────

    def update_persona(self, prompt: str | None, version: int | None = None) -> None:
        """Replace the persona used for subsequent executions.

        The new persona — including an edited MEMORY — takes effect on the
        next message because ``_build_options`` re-injects the live
        ``self._persona`` via ``--append-system-prompt`` every turn.
        Resumed Telegram sessions are *not* torn down on a version bump:
        the agent's own MEMORY writes bump the persona version constantly,
        and dropping the conversation each time wiped the user's context
        mid-chat (see ``_resolve_resume_target``).  ``version`` is tracked
        only so newly created sessions record which version they started
        under, for diagnostics.

        A ``None`` ``prompt`` is interpreted in two distinct ways,
        disambiguated by ``version``:

        * ``version is None`` — the server reports no active persona at
          all.  This is the valid "no persona configured" steady state
          (e.g. operator removed the persona); we clear the cached
          persona and return without raising, mirroring what
          ``superpos_agent_core.main.run_agent`` does at startup.
          ``_persona_version`` is left untouched because there is no new
          version to record.
        * ``version is not None`` — the server reports a real live
          persona version but the assembled prompt came back ``None``.
          ``superpos_agent_core.superpos_client.get_persona_assembled``
          swallows both 404s and transport errors and returns ``None`` in
          either case, so this combination can only mean a fetch failure
          (a server that reports a live version cannot simultaneously
          have no configured persona).  We raise
          ``PersonaRefreshFailed`` and leave the cached persona / version
          untouched.  The core poller wraps its refresh block in a broad
          ``except Exception``, so the raise aborts the follow-up
          ``persona_version = server_version`` assignment; the next poll
          cycle still sees ``changed == True`` and retries the fetch
          instead of leaving Claude running indefinitely with no persona
          at all.  Preserving the cached persona also keeps the system
          prompt intact for resumed Claude sessions during the transient
          failure window.

        Note: re-syncing SubAgentDefinitions after a persona bump is owned
        by ``superpos_agent_core.superpos_poller._resync_sub_agents``,
        which the core poller calls in a background thread on the same
        event.  Kicking off a second sync from here would race with that
        one on the same ``.claude/subagents`` tree (duplicate HTTP traffic
        plus file-write contention), so this method only updates the
        in-memory persona/version state.
        """
        if prompt is None and version is not None:
            raise PersonaRefreshFailed(
                f"Persona v{version} fetch returned None; the server reports a "
                "real persona exists but the assembled prompt could not be "
                "loaded. Keeping cached persona so resumed Claude sessions still "
                "see a system prompt; the poller's outer except will abort its "
                "``persona_version = server_version`` assignment, so the next "
                "poll cycle will retry the fetch."
            )
        self._persona = prompt
        if version is not None:
            self._persona_version = version

    def clear_session(self, chat_id: int | str) -> None:
        self._sessions.clear(chat_id)

    def model_info(self) -> dict[str, str]:
        """Current model/effort, reported to Superpos on each heartbeat.

        Reads live runtime state so mid-session ``/model`` / ``/effort``
        switches surface on the dashboard.
        """
        return {"model": self._runtime.model, "effort": self._runtime.effort}

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

        Worktree-scoped sessions live under their own
        ``projects/<encoded-cwd>/`` dir (one per branch), so we scan every
        subdirectory of ``projects/`` rather than only the main workspace
        project.  Otherwise idle worktree sessions accumulate unbounded
        and active ones risk deletion if the agent later runs on a
        different branch.
        """
        counts = {"projects": 0, "session_env": 0, "bytes_freed": 0}
        cutoff = time.time() - (max_age_hours * 3600)
        preserve: set[str] = self._sessions.active_session_ids()

        claude_dir = os.path.join(os.environ.get("HOME", "/tmp"), ".claude")

        def _session_id_from_name(name: str) -> str:
            # Both `<sid>` (dir) and `<sid>.jsonl` (transcript) live in projects/.
            return name[:-6] if name.endswith(".jsonl") else name

        projects_root = os.path.join(claude_dir, "projects")
        if os.path.isdir(projects_root):
            for project_name in os.listdir(projects_root):
                projects_dir = os.path.join(projects_root, project_name)
                if not os.path.isdir(projects_dir):
                    continue
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

    def _resolve_slot(self, branch: str | None) -> str:
        """Return the worktree lock key for ``branch``.

        Callers pass the *effective* branch — i.e. the branch the
        execution will actually run on after any SessionStore-based
        restoration — so the lock key matches the cwd used by
        ``_execute_inner``.  Passing ``req.branch`` directly is unsafe
        for resumed Telegram turns where the effective branch comes
        from the stored session.
        """
        if (
            branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            return worktree_path(self._config.executor_working_dir, branch)
        return "__main__"

    # ── Resume target resolution ─────────────────────────────────────────

    def _peek_effective_branch(
        self, req: ExecutionRequest,
    ) -> str | None:
        """Branch the slot lock should key off, *without* mutating state.

        Called from ``_run_one`` before the worktree lock is acquired
        so the slot we lock matches the cwd ``_execute_inner`` will
        actually use.  Reads — but does not clear — the SessionStore
        entry, leaving full resolution (persona invalidation, resume
        id) to :meth:`_resolve_resume_target`, which fires *after* the
        lock so it observes whatever a preceding task for this chat
        wrote back.

        In the rare case where persona invalidation will fire post-lock
        and reset the effective branch, the slot picked here can be a
        miss — accepted as a minor scheduling cost.  The common case
        (no persona bump between peek and resolve) sees peek and
        resolution agree on the branch.
        """
        if req.branch is not None or req.source != "telegram":
            return req.branch
        stored = self._sessions.get_with_version(req.chat_key)
        if stored is None:
            return None
        _, _, stored_branch = stored
        return stored_branch

    def _resolve_resume_target(
        self, req: ExecutionRequest,
    ) -> tuple[str | None, str | None]:
        """Return ``(resume_id, effective_branch)`` for this request.

        Reads the SessionStore for Telegram chats and restores the branch
        the session was started on when the request carries no explicit
        branch (no ``--branch`` token, no PR ref in the text), so cwd
        matches the original transcript at
        ``~/.claude/projects/<encoded-cwd>/<sid>.jsonl``.

        We resume regardless of the persona version the session was
        created under.  The live persona — including a freshly edited
        MEMORY — is re-injected into every turn via
        ``--append-system-prompt`` (see ``_build_options``), so a
        persona-version bump never needs a fresh session.  The agent's
        own MEMORY writes bump that version constantly, so invalidating
        on every bump wiped the user's context mid-chat; this matches the
        Codex/Gemini/Qwen agents, which re-read their persona file each
        turn and never tear down the conversation.

        Called by ``_run_one`` *before* slot resolution so the worktree
        lock key matches the cwd ``_execute_inner`` will actually use.
        Non-Telegram sources (Superpos tasks) skip the SessionStore
        entirely and pass ``req.branch`` through unchanged.  Call it
        exactly once per request — ``_run_one`` does so and threads the
        result into ``_execute`` / ``_execute_inner`` via ``pre_resolved``.
        """
        if req.source != "telegram":
            return None, req.branch
        stored = self._sessions.get_with_version(req.chat_key)
        if stored is None:
            return None, req.branch
        resume_id, _stored_version, stored_branch = stored
        effective_branch = req.branch
        if req.branch is None and stored_branch is not None:
            log.info(
                "Resuming session for chat %s on stored branch %r "
                "(req.branch was None)",
                req.chat_key, stored_branch,
            )
            effective_branch = stored_branch
        return resume_id, effective_branch

    # ── Main consumer loop ───────────────────────────────────────────────

    async def _acquire_lock_with_expiry(
        self, lock: asyncio.Lock, claim_expired: asyncio.Event,
    ) -> bool:
        """Acquire ``lock`` or bail when ``claim_expired`` fires.

        Returns ``True`` when the caller now holds the lock.  If
        ``claim_expired`` won the race, the lock is released (if it
        had also acquired) and ``False`` is returned.
        """
        lock_task = asyncio.create_task(lock.acquire())
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
                lock.release()
            return False
        return True

    async def _run_one(self, req: ExecutionRequest) -> None:
        claim_expired = asyncio.Event()
        progress_task: asyncio.Task | None = None

        # Two-phase lookup.  The slot lock has to key off the effective
        # branch (otherwise a resumed turn with ``req.branch=None`` but
        # ``stored_branch=X`` would lock ``__main__`` while running in
        # the X worktree), so we peek the branch up front for the slot
        # key.  But the resume id has to come from SessionStore *after*
        # the lock is held — otherwise a follow-up message would
        # snapshot the stored id at queue time and miss the newer id
        # the preceding task for this chat wrote when it finished.
        slot_branch = self._peek_effective_branch(req)

        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            progress_task = asyncio.create_task(
                report_progress(self._superpos, req.superpos_task_id, claim_expired)
            )

        try:
            async with self._semaphore:
                if claim_expired.is_set():
                    log.warning(
                        "Claim expired while waiting for semaphore: %s",
                        req.superpos_task_id,
                    )
                    return

                # Acquire the worktree lock, then re-resolve under it.
                # If a same-chat task wrote SessionStore during the
                # wait, the post-lock effective branch may not match
                # the peeked slot — execution would then run in branch
                # Y while serialized on branch X's lock, defeating the
                # per-worktree mutex.  Release and retry on the
                # canonical slot.  Converges in ≤2 iterations in
                # practice (one wait, one resolve, maybe one swap).
                wt_lock: asyncio.Lock | None = None
                lock_acquired = False
                pre_resolved: tuple[str | None, str | None] | None = None
                try:
                    for attempt in range(_MAX_SLOT_RESOLVE_ATTEMPTS):
                        slot = self._resolve_slot(slot_branch)
                        wt_lock = self._get_worktree_lock(slot)
                        if not await self._acquire_lock_with_expiry(
                            wt_lock, claim_expired,
                        ):
                            log.warning(
                                "Claim expired while waiting for worktree lock: %s",
                                req.superpos_task_id,
                            )
                            return

                        pre_resolved = self._resolve_resume_target(req)
                        _, effective_branch = pre_resolved
                        if self._resolve_slot(effective_branch) == slot:
                            lock_acquired = True
                            break

                        # Effective branch diverged from the slot we
                        # locked — a same-chat task updated
                        # SessionStore mid-wait.  Release and reacquire
                        # on the now-canonical slot so execution and
                        # serialization agree.
                        log.info(
                            "Worktree slot %r diverged from resolved branch %r; "
                            "swapping lock (attempt %d)",
                            slot, effective_branch, attempt + 1,
                        )
                        wt_lock.release()
                        wt_lock = None
                        slot_branch = effective_branch
                    else:
                        # Loop exhausted — SessionStore churn for this
                        # chat is keeping the slot moving.  Bail rather
                        # than spin: another concurrent same-chat task
                        # holds the canonical slot, and forcing through
                        # would defeat the serialization guarantee.
                        log.warning(
                            "Worktree slot kept diverging for chat %s after %d "
                            "attempts; skipping execution to avoid lock mismatch",
                            req.chat_id, _MAX_SLOT_RESOLVE_ATTEMPTS,
                        )
                        return

                    await self._execute(req, claim_expired, pre_resolved=pre_resolved)
                finally:
                    if lock_acquired and wt_lock is not None:
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

    async def _backoff_sleep(
        self, wait: float, progress_event: asyncio.Event | None,
    ) -> None:
        """Sleep for ``wait`` seconds while keeping the stall watchdog happy.

        Retry backoffs (API 500, rate-limit, CLI crash) are intentional idle
        windows — without a heartbeat the watchdog cancels healthy runs when
        ``claude_stall_timeout`` is set below the backoff duration.  Pulse
        the event in chunks well under the stall timeout so the watchdog
        only fires for genuine deadlocks (Codex P2).
        """
        if progress_event is None or wait <= 0:
            await asyncio.sleep(wait)
            return
        progress_event.set()
        # Chunk must be smaller than stall_timeout so the watchdog never
        # fires mid-backoff, but not so small we burn CPU on a busy-loop
        # of sleeps for very small stall_timeout configs (e.g. in tests).
        stall = float(self._config.claude_stall_timeout)
        upper = max(0.05, stall / 2)
        chunk = max(0.05, min(upper, 15.0, wait))
        remaining = wait
        while remaining > 0:
            slice_ = min(chunk, remaining)
            await asyncio.sleep(slice_)
            remaining -= slice_
            progress_event.set()

    async def _report_stall(
        self,
        req: ExecutionRequest,
        streamer: TelegramStreamer,
        stall_timeout: int,
    ) -> None:
        """Notify the user and Superpos that a task was killed for stalling."""
        msg = (
            f"Task aborted: the Claude subprocess produced no output for "
            f"{stall_timeout}s and was forcibly cancelled."
        )
        try:
            await streamer.error(msg)
        except Exception:
            log.debug("Failed to send stall notification to Telegram", exc_info=True)
        if req.source == "superpos" and req.superpos_task_id and self._superpos:
            try:
                await self._superpos.fail_task(req.superpos_task_id, msg)
            except Exception:
                log.debug(
                    "Failed to mark task %s as failed after stall",
                    req.superpos_task_id, exc_info=True,
                )
    async def _execute(
        self,
        req: ExecutionRequest,
        claim_expired: asyncio.Event,
        retries: int = 3,
        *,
        pre_resolved: tuple[str | None, str | None] | None = None,
    ) -> None:
        """Run a single request to completion.

        ``pre_resolved`` is the ``(resume_id, effective_branch)`` pair
        already computed by ``_run_one``.  Threading it through avoids
        a redundant SessionStore lookup in ``_execute_inner`` (and the
        risk of repeating the persona-invalidation side effect).
        Direct callers — e.g. unit tests — may omit it and let the
        inner method fall back to ``_resolve_resume_target`` itself.
        """
        self._active_count += 1
        if self._active_count == 1 and self._superpos:
            try:
                await self._superpos.update_status("busy")
            except Exception:
                log.debug("Failed to set agent status to busy")

        streamer = TelegramStreamer(self._gateway, req.chat_id, thread_id=req.thread_id)
        try:
            await streamer.start()
        except Exception:
            log.debug("Streamer start failed (non-fatal)")

        inner_task: asyncio.Task | None = None
        watcher_task: asyncio.Task | None = None
        stall_watchdog: asyncio.Task | None = None
        progress_event = asyncio.Event()
        # Set while one or more interactive AskUserQuestions are parked
        # awaiting a human reply.  No SDK messages flow during that window, so
        # the stall watchdog must not treat the silence as a deadlock; the
        # claim/zombie guards likewise tolerate the wait.  _execute_inner
        # pulses this via the pause/resume callbacks threaded into the ask MCP
        # tool context.  Reference-counted (not a bare flag) so concurrent ask
        # calls nest: if Claude emits two ask tool calls and the second fails
        # fast with AskAlreadyPending, its resume must not un-pause the
        # watchdog while the first question is still legitimately waiting.
        ask_pending = asyncio.Event()
        ask_pending_count = 0
        stalled = False

        async def _watch_claim_expiry() -> None:
            await claim_expired.wait()
            if inner_task is not None:
                inner_task.cancel()

        stall_timeout = self._config.claude_stall_timeout

        async def _watch_stall() -> None:
            nonlocal stalled
            while True:
                try:
                    await asyncio.wait_for(
                        progress_event.wait(), timeout=stall_timeout,
                    )
                except asyncio.TimeoutError:
                    # A parked question (awaiting a human) is not a stall —
                    # the human may legitimately take minutes.  Keep waiting
                    # without cancelling; the ask itself has its own timeout.
                    if ask_pending.is_set():
                        continue
                    stalled = True
                    log.warning(
                        "Claude SDK iterator stalled for %ds on task %s "
                        "(no message yielded) — cancelling subprocess",
                        stall_timeout, req.superpos_task_id or req.chat_id,
                    )
                    if inner_task is not None and not inner_task.done():
                        inner_task.cancel()
                    return
                progress_event.clear()

        def _pause_watchdog() -> None:
            """Suspend the stall watchdog while a question is parked.

            Reference-counted: the watchdog pauses on the 0→1 transition and
            stays paused until every parked question has resumed.  Also pulses
            ``progress_event`` (and, for superpos tasks, lets the claim-expiry
            watcher's heartbeat keep flowing) so neither the stall timer nor
            the claim watchdog fires during the human wait.
            """
            nonlocal ask_pending_count
            ask_pending_count += 1
            ask_pending.set()
            progress_event.set()

        def _resume_watchdog() -> None:
            nonlocal ask_pending_count
            if ask_pending_count > 0:
                ask_pending_count -= 1
            # Only un-pause once the last outstanding question resolves — a
            # second, AskAlreadyPending-failing call decrements its own
            # increment but leaves the owning question's pause in effect.
            if ask_pending_count == 0:
                ask_pending.clear()
            # Treat the answer as fresh progress so the stall deadline resets
            # from now rather than from before the (long) human pause.
            progress_event.set()

        try:
            inner_task = asyncio.create_task(
                self._execute_inner(
                    req, streamer, retries,
                    pre_resolved=pre_resolved,
                    progress_event=progress_event,
                    pause_watchdog=_pause_watchdog,
                    resume_watchdog=_resume_watchdog,
                ),
            )
            # Register with the base class so the /stop Telegram command can
            # find and cancel this in-flight work via cancel_chat(chat_key) —
            # keyed per forum topic so /stop in one topic leaves the others
            # running.  Auto-untracks via done callback — no cleanup needed
            # in finally.
            self._track_chat_task(req.chat_key, inner_task)
            if req.source == "superpos" and req.superpos_task_id:
                watcher_task = asyncio.create_task(_watch_claim_expiry())
            stall_watchdog = asyncio.create_task(_watch_stall())
            try:
                # Safety net against zombie pipes where the Claude subprocess
                # dies but grandchildren keep stdout open, hanging the
                # async-for loop forever.  A parked AskUserQuestion is a
                # legitimate long wait, so when the guard elapses while one is
                # pending we re-arm it instead of cancelling — the ask has its
                # own timeout, after which messages flow again and the guard
                # behaves normally.
                max_timeout = self._config.executor_max_turns * 120  # ~2min/turn
                while True:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(inner_task), timeout=max_timeout,
                        )
                        break
                    except asyncio.TimeoutError:
                        if ask_pending.is_set():
                            continue
                        raise
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
                # Explicitly fail the task on the server so it moves to a
                # terminal state instead of lingering "in_progress" until the
                # server's separate progress_timeout fires.  Without this,
                # the dashboard kept showing the task at 95% (heartbeat cap)
                # for the full server-side reclaim window, even though the
                # agent had already given up locally.  Skip if the claim has
                # already expired — the server knows we don't own it anymore.
                if (
                    req.source == "superpos"
                    and req.superpos_task_id
                    and self._superpos
                    and not claim_expired.is_set()
                ):
                    try:
                        await self._superpos.fail_task(
                            req.superpos_task_id,
                            f"Execution timed out after {max_timeout}s "
                            f"(possible zombie pipe — Claude SDK iterator hung).",
                        )
                    except Exception:
                        log.debug(
                            "Failed to mark task %s as failed after timeout",
                            req.superpos_task_id, exc_info=True,
                        )
                    timeout_detail = (
                        f"Execution timed out after {max_timeout}s "
                        f"(possible zombie pipe — Claude SDK iterator hung)."
                    )
                    self._recent_tasks.record(
                        req.chat_id,
                        TaskSummary(
                            task_id=req.superpos_task_id,
                            description=req.prompt[:200],
                            outcome="failed",
                            detail=timeout_detail,
                        ),
                    )
            except asyncio.CancelledError:
                if claim_expired.is_set():
                    log.warning(
                        "Execution aborted: claim expired for superpos task %s",
                        req.superpos_task_id,
                    )
                elif stalled:
                    log.warning(
                        "Execution aborted: claude subprocess stalled for "
                        "%ds on task %s",
                        stall_timeout, req.superpos_task_id or req.chat_id,
                    )
                    await self._report_stall(req, streamer, stall_timeout)
                else:
                    raise
        finally:
            if watcher_task:
                watcher_task.cancel()
                try:
                    await watcher_task
                except asyncio.CancelledError:
                    pass
            if stall_watchdog:
                stall_watchdog.cancel()
                try:
                    await stall_watchdog
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
                report_progress(self._superpos, task_id, claim_expired)
            )

        full_text = ""

        async def _run_inner() -> None:
            nonlocal full_text
            # Background tasks have no Telegram round-trip context and don't
            # stream, so the interactive ask path can't route a question.
            # Disable it: plain no-ask options (no superpos_ask server, no
            # steering note, native AskUserQuestion left enabled).
            options = self._build_options(enable_ask=False)
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

    def _ask_enabled(self, enable_ask: bool) -> bool:
        """Whether the interactive ask wiring is actually active for a run.

        The AskUserQuestion-over-Telegram feature requires the streaming
        control protocol plus the in-process ``superpos_ask`` SDK MCP server.
        That combination only works on a *native* Anthropic backend: on an
        Anthropic-compatible shim (MiniMax/Kimi) the in-process SDK server +
        external stdio MCP under the streaming control protocol fails the
        CLI startup handshake (pre-init ``CLIConnectionError`` → crash-loop;
        regression from #34).  Ask also makes no sense without a caller that
        opted in (``enable_ask``), so both must hold.
        """
        return enable_ask and self._config.is_native_anthropic

    def _build_options(
        self,
        resume_session: str | None = None,
        cwd: str | None = None,
        system_prompt_append: str | None = None,
        *,
        enable_ask: bool = True,
        strip_optional_mcp: bool = False,
    ) -> ClaudeCodeOptions:
        """Assemble ``ClaudeCodeOptions`` for a query.

        ``enable_ask`` controls the interactive AskUserQuestion-over-Telegram
        wiring.  When True (interactive ``_execute_inner`` path) *and* the
        backend is native Anthropic (see :meth:`_ask_enabled`), the in-process
        ``superpos_ask`` MCP server is registered, the native AskUserQuestion
        tool is disabled, and a steering note nudges the model toward the MCP
        tool.  Background tasks (``run_background``) pass False, and shim/
        minimax backends are gated off regardless: they never publish an
        ``_AskContext`` / can't survive the streaming handshake, so a parked
        question could not be routed — they get a plain no-ask path instead
        (no ask server, no steering note, native AskUserQuestion left
        untouched, string-prompt mode).

        ``strip_optional_mcp`` (degrade safeguard) drops every optional MCP
        server (ask + shim search) so a pre-init MCP startup crash can retry
        without the tooling that failed the handshake instead of looping the
        identical config forever.
        """
        ask_active = self._ask_enabled(enable_ask)
        opts: dict = {
            "model": self._runtime.model,
            "max_turns": self._config.executor_max_turns,
            "permission_mode": "bypassPermissions",
            "cwd": cwd or self._config.executor_working_dir,
            # --debug-to-stderr makes the CLI write its failure reason (API
            # overload, rate limit, auth) to stderr, which the open_process
            # patch captures to a tempfile.  Without it a CLI crash exits 1
            # with an empty stderr and we can only log "exit code 1".
            #
            # The SDK's SubprocessCLITransport passes options.debug_stderr to
            # open_process whenever "debug-to-stderr" is in extra_args, and
            # ClaudeCodeOptions.debug_stderr defaults to sys.stderr.  That
            # would route the crash output straight to the container stderr,
            # and our open_process patch only installs the tempfile capture
            # when stderr is None.  Force debug_stderr=None so the SDK passes
            # stderr=None and the capture (not the SDK) owns the fd.
            "debug_stderr": None,
            "extra_args": {
                "effort": self._runtime.effort,
                "debug-to-stderr": None,
            },
        }
        # The interactive ask path needs the in-process superpos_ask MCP
        # server; background tasks (enable_ask=False) and shim/minimax backends
        # get the plain server set without it so the native AskUserQuestion is
        # never shadowed by a tool that can't route its question (no
        # _AskContext, no streaming) — and so the SDK control-protocol startup
        # handshake doesn't choke on the in-process server on a shim backend.
        if ask_active:
            mcp_servers = dict(self._mcp)
        else:
            mcp_servers = {
                k: v for k, v in self._mcp.items() if k != _ASK_MCP_SERVER
            }
        # Degrade safeguard: a pre-init MCP startup crash retries with every
        # optional MCP server stripped (ask + shim search) rather than looping
        # the identical failing config.
        if strip_optional_mcp:
            mcp_servers = {
                k: v for k, v in mcp_servers.items()
                if k not in _OPTIONAL_MCP_SERVERS
            }
        if mcp_servers:
            opts["mcp_servers"] = mcp_servers
        if resume_session:
            opts["resume"] = resume_session
        # Steer the model away from the native AskUserQuestion tool (handled
        # opaquely inside the CLI subprocess, so it never reaches Telegram)
        # toward our in-process MCP tool, which renders an inline keyboard and
        # feeds the answer back as the tool result.  Only on the interactive
        # path — background tasks leave AskUserQuestion enabled and skip the
        # steering note.
        disallowed: list[str] = []
        parts = []
        if self._persona:
            parts.append(self._persona)
        if ask_active:
            disallowed.append("AskUserQuestion")
            parts.append(
                "## Asking the user a question\n"
                "When you need the user to choose between options or confirm a "
                f"direction, call the `{_ASK_MCP_TOOL_FQN}` tool (NOT the "
                "native AskUserQuestion tool, which is disabled). It renders "
                "interactive buttons in the chat and returns the user's "
                "selection. Pass a `questions` array, each with a `header`, "
                "the `question` text, and 1-4 `options` (each "
                "`{label, description, preview?}`); set `multiSelect: true` to "
                "allow multiple picks. If the result is marked `timed_out`, "
                "the user did not respond — proceed sensibly without their "
                "input."
            )
        # On a shim backend, Anthropic's hosted WebSearch/WebFetch 400.
        # Disable them so the model stops calling dead tools, and point it
        # at the replacement search MCP when one was wired at startup.
        if self._shim_backend:
            disallowed += ["WebSearch", "WebFetch"]
            if self._search_note:
                parts.append(self._search_note)
        opts["disallowed_tools"] = disallowed
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
        self,
        req: ExecutionRequest,
        streamer: TelegramStreamer,
        retries: int,
        *,
        pre_resolved: tuple[str | None, str | None] | None = None,
        progress_event: asyncio.Event | None = None,
        pause_watchdog=None,
        resume_watchdog=None,
    ) -> None:
        t0 = time.monotonic()
        full_text = ""

        # Publish this run's chat context (and the watchdog pause/resume
        # hooks) so the shared ask_user MCP tool handler can route a question
        # to the right Telegram chat/topic and suspend the stall watchdog
        # while it's parked.  Defaults are no-ops so direct callers (tests)
        # without a watchdog still work.
        def _noop() -> None:
            pass

        ask_ctx = _AskContext(
            chat_id=req.chat_id,
            thread_id=req.thread_id,
            pause=pause_watchdog or _noop,
            resume=resume_watchdog or _noop,
        )
        ask_ctx_token = _ask_context_var.set(ask_ctx)

        # ``_run_one`` resolves the resume target before slot/lock
        # acquisition so the lock key matches the cwd we use here.
        # When called via that path ``pre_resolved`` carries the
        # result; direct callers (unit tests, future entrypoints) fall
        # back to the helper.  Calling the helper twice for one
        # request is safe — persona invalidation already cleared the
        # entry on the first call — but it's redundant work, so
        # prefer pre_resolved.
        if pre_resolved is not None:
            resume_id, effective_branch = pre_resolved
        else:
            resume_id, effective_branch = self._resolve_resume_target(req)

        cwd_override: str | None = None
        if (
            effective_branch
            and self._config.executor_worktree_isolation
            and is_git_repo(self._config.executor_working_dir)
        ):
            try:
                cwd_override = await ensure_worktree(
                    self._config.executor_working_dir, effective_branch,
                )
            except Exception:
                log.warning(
                    "Failed to create worktree for branch %r; falling back to default cwd",
                    effective_branch, exc_info=True,
                )

        # The branch we pin on the SessionStore entry must match where the
        # transcript actually lives.  Claude CLI writes the transcript under
        # ``~/.claude/projects/<encoded-cwd>/<sid>.jsonl``, so a future
        # resume that restores cwd from the stored branch will look in the
        # wrong project dir whenever execution didn't actually run in the
        # worktree (ensure_worktree raised, isolation disabled, or the repo
        # isn't git).  In those cases the transcript lives under the default
        # cwd's project dir; pin branch=None so future resumes resolve to
        # the same default cwd and find it.
        session_branch = effective_branch if cwd_override else None

        system_prompt_append: str | None = None
        wt_base = self._config.executor_working_dir
        if not effective_branch and is_git_repo(wt_base):
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

        if req.source == "telegram":
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

        # The interactive ask wiring (in-process superpos_ask SDK MCP server +
        # streaming control protocol) only works on a native Anthropic backend;
        # on a shim/minimax backend it fails the CLI startup handshake (pre-init
        # CLIConnectionError → crash-loop; regression from #34).  Gate streaming
        # on the same flag _build_options uses for the ask server so the two
        # stay in lockstep: native → streaming + ask, shim → string prompt + no
        # ask (the proven pre-#34 path).
        ask_active = self._ask_enabled(True)

        # Attempt-scoped degrade flag for the durable safeguard (B): set once
        # after a pre-init MCP startup crash so the next attempt strips the
        # optional MCP servers (ask/search) and runs degraded.  Bounded — only
        # flips False→True a single time; if a degraded attempt still crashes
        # pre-init it falls through to the normal retry/backoff below rather
        # than oscillating strip/re-add.
        degraded = False

        # Streaming-input mode: ``query`` takes an ``AsyncIterable[dict]`` of
        # user messages rather than a bare string.  This is required for the
        # in-process MCP round-trip (the ask_user tool) and the SDK control
        # protocol — under the old one-shot string form the CLI owns tool
        # execution end-to-end and our tool handler never runs.  We yield the
        # single user message, matching the SDK's documented shape.  Only used
        # when ask is active; otherwise we pass the bare string (lighter, and
        # what shim backends ran on healthily pre-#34).
        def _prompt_stream(text: str):
            async def _gen():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": text},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                }
            return _gen()

        try:
            for attempt in range(1, retries + 1):
                capture: dict = {}
                capture_token = _stderr_capture_var.set(capture)
                try:
                    # Streaming is only safe/needed when ask is active; a
                    # degraded retry (optional MCPs stripped) has no ask server,
                    # so drop back to the string prompt too.
                    stream_mode = ask_active and not degraded
                    options = self._build_options(
                        resume_session=resume_id,
                        cwd=cwd_override,
                        system_prompt_append=system_prompt_append,
                        enable_ask=stream_mode,
                        strip_optional_mcp=degraded,
                    )
                    query_prompt = (
                        _prompt_stream(effective_prompt)
                        if stream_mode else effective_prompt
                    )
                    async for message in query(
                        prompt=query_prompt, options=options,
                    ):
                        # Signal the stall watchdog that the SDK iterator is
                        # alive — every yielded message resets the deadline.
                        if progress_event is not None:
                            progress_event.set()
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
                                        req.chat_key, sid, self._persona_version,
                                        branch=session_branch,
                                    )
                        elif isinstance(message, ResultMessage) and hasattr(message, "session_id"):
                            sid = message.session_id
                            if sid:
                                last_session_id = sid
                                if req.source == "telegram":
                                    self._sessions.set_with_version(
                                        req.chat_key, sid, self._persona_version,
                                        branch=session_branch,
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
                    # A CLI subprocess crash often arrives wrapped in a
                    # ``BaseExceptionGroup`` from the anyio TaskGroup teardown,
                    # whose ``str()`` is just "unhandled errors in a TaskGroup".
                    # Flatten to the leaves and build a combined message (top
                    # level + every leaf's str() and type name) so the substring
                    # classifications below match signals living in a leaf.
                    leaves = _flatten_exception(e)
                    err_str = "\n".join(
                        [str(e)]
                        + [f"{type(leaf).__name__}: {leaf}" for leaf in leaves]
                    )
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
                        await self._backoff_sleep(wait, progress_event)
                        continue

                    # CLI subprocess crash — resume the session so completed tool
                    # calls aren't re-run.  Test the leaves too: the SDK wraps
                    # the real CLIConnectionError/ProcessError inside an
                    # ExceptionGroup, so a top-level isinstance check misses it.
                    is_process_error = (
                        isinstance(e, ProcessError)
                        or any(isinstance(x, ProcessError) for x in leaves)
                    )
                    is_connection_error = (
                        isinstance(e, CLIConnectionError)
                        or any(isinstance(x, CLIConnectionError) for x in leaves)
                    )
                    is_cli_crash = (
                        is_process_error
                        or is_connection_error
                        or "ProcessTransport is not ready" in err_str
                        or "Command failed with exit code" in err_str
                    )
                    # The exit code may live on a ProcessError leaf when the
                    # crash is group-wrapped, so fall back to scanning leaves.
                    process_leaf = next(
                        (x for x in leaves if isinstance(x, ProcessError)),
                        e if isinstance(e, ProcessError) else None,
                    )
                    exit_code = getattr(process_leaf, "exit_code", "?")
                    if is_cli_crash and last_session_id and attempt < retries:
                        wait = 5 * attempt
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
                        await self._backoff_sleep(wait, progress_event)
                        resume_id = last_session_id
                        effective_prompt = (
                            "Your previous CLI invocation crashed before completing this task. "
                            "Review your prior outputs in this session to see what was already done, "
                            "then continue from where you left off and finish the task."
                        )
                        continue

                    # Durable safeguard (B): a pre-init crash while optional MCP
                    # servers were in play (the ask SDK server and/or the shim
                    # search MCP) is most likely the MCP/streaming startup
                    # handshake choking — exactly the #34 regression.  Rather
                    # than loop the identical failing config forever, degrade
                    # once: strip the optional MCP servers (and drop streaming,
                    # which was only needed for the ask server) and retry from
                    # scratch.  ``degraded`` is attempt-scoped and flips
                    # False→True a single time; if the degraded attempt still
                    # crashes pre-init it falls through to the plain pre-init
                    # retry below, so we never oscillate strip↔re-add.
                    optional_mcp_present = (
                        not degraded
                        and bool(
                            (ask_active and _ASK_MCP_SERVER in self._mcp)
                            or (set(self._mcp) & _OPTIONAL_MCP_SERVERS)
                        )
                    )
                    if (
                        is_cli_crash
                        and not last_session_id
                        and attempt < retries
                        and not full_text.strip()
                        and optional_mcp_present
                    ):
                        degraded = True
                        wait = 5 * attempt
                        log.warning(
                            "Claude CLI crashed before init (exit %s, attempt "
                            "%d/%d) with optional MCP servers present; retrying "
                            "WITHOUT ask/search MCPs in %ds. CLI stderr:\n%s",
                            exit_code, attempt, retries, wait,
                            captured_stderr or "(no stderr captured)",
                        )
                        stderr_blurb = (
                            f"\nCLI stderr tail:\n{captured_stderr}"
                            if captured_stderr else ""
                        )
                        await streamer.append(
                            "\n⚠️ MCP failed at startup; running without "
                            f"ask/search this attempt.{stderr_blurb}\n",
                        )
                        await self._backoff_sleep(wait, progress_event)
                        resume_id = None
                        effective_prompt = prompt_text
                        continue

                    # Pre-``init`` crash: the CLI died before emitting a session
                    # id (e.g. OOM at startup), so there's nothing to resume.
                    # With no output produced yet there are no side effects to
                    # duplicate, so retry from scratch with the original prompt
                    # (no resume, no "continue your session" framing).
                    if (
                        is_cli_crash
                        and not last_session_id
                        and attempt < retries
                        and not full_text.strip()
                    ):
                        wait = 5 * attempt
                        log.warning(
                            "Claude CLI crashed before init (exit %s, attempt %d/%d); "
                            "retrying from scratch in %ds. CLI stderr:\n%s",
                            exit_code, attempt, retries, wait,
                            captured_stderr or "(no stderr captured)",
                        )
                        stderr_blurb = (
                            f"\nCLI stderr tail:\n{captured_stderr}"
                            if captured_stderr else ""
                        )
                        await streamer.append(
                            f"\n⏳ CLI crashed before starting (exit {exit_code}), "
                            f"retrying in {wait}s...{stderr_blurb}\n",
                        )
                        await self._backoff_sleep(wait, progress_event)
                        resume_id = None
                        effective_prompt = prompt_text
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
                        await self._backoff_sleep(wait, progress_event)
                        continue
                    elif resume_id and attempt < retries:
                        log.warning("Session resume failed, retrying with fresh session")
                        self._sessions.clear(req.chat_key)
                        resume_id = None
                        continue

                    if isinstance(e, ClaudeSDKError):
                        log.error("Claude SDK error: %s", e)
                    else:
                        log.exception("Unexpected error during execution")
                    hint = ""
                    # Fire for the wrapped case too: ``is_cli_crash`` already
                    # accounts for ProcessError/CLIConnectionError leaves and the
                    # exit-code/ProcessTransport signatures, so the OOM guidance
                    # reaches the user even when the crash is group-wrapped.
                    if is_cli_crash:
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
                    # The raw top-level ``str(e)`` for a group-wrapped crash is
                    # the opaque "unhandled errors in a TaskGroup" — surface the
                    # flattened message instead so the real cause is visible.
                    surfaced = err_str if is_cli_crash else str(e)
                    try:
                        await streamer.error(f"Error: {surfaced}{hint}")
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

        finally:
            # Run isolation already scopes the contextvar per task, but
            # reset explicitly so direct callers (tests) don't leak the
            # ask context across invocations.
            try:
                _ask_context_var.reset(ask_ctx_token)
            except (ValueError, LookupError):
                pass

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
