"""Entry point: runs all daemons via asyncio.gather()."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import shutil
import signal
import sys

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKError, query

from .superpos_client import SuperposClient
from .superpos_poller import run_superpos_poller
from .claude_executor import ClaudeExecutor
from .config import Config
from .runtime_config import RuntimeConfig
from .telegram_bot import build_telegram_app, run_telegram_bot
from .telegram_gateway import TelegramGateway
from .worktree_manager import is_git_repo, prune_worktrees

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_LOG_DIR = os.path.join(os.environ.get("HOME", "/tmp"), ".claude", "logs")

os.makedirs(_LOG_DIR, exist_ok=True)

# Console (stderr) — same as before
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, stream=sys.stderr)

# Persistent file — survives container restart via the /home/agent/.claude volume
_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(_LOG_DIR, "agent.log"),
    maxBytes=5 * 1024 * 1024,  # 5 MB per file
    backupCount=3,  # keep agent.log, agent.log.1, agent.log.2, agent.log.3
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_file_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(_file_handler)

log = logging.getLogger(__name__)


_AUTH_HELP_OAUTH_EXPIRED = """
╔══════════════════════════════════════════════════════════════╗
║       Claude OAuth session expired — cannot start           ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Your OAuth session has fully expired. Re-authenticate:      ║
║                                                              ║
║    docker run -it \\                                         ║
║      -v claude_auth:/home/agent/.claude \\                   ║
║      --entrypoint claude superpos-claude-agent                   ║
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
║      --entrypoint claude superpos-claude-agent                   ║
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
    """Return the appropriate help text if err is a Claude auth failure, else None."""
    if "OAuth token has expired" in err or "oauth" in err.lower() and "expired" in err.lower():
        return _AUTH_HELP_OAUTH_EXPIRED
    if "authentication_error" in err or "Invalid authentication credentials" in err:
        return _AUTH_HELP_INVALID_KEY
    return None


_REQUIRED_PERMISSIONS = ("tasks:read", "tasks:claim", "tasks:update")
_OPTIONAL_PERMISSIONS = ("tasks:create", "knowledge:write")


async def _warn_missing_permissions(gateway: TelegramGateway | None, config: Config) -> None:
    """If the agent's /me profile lacks critical permissions, log and notify.

    Runs as a one-shot coroutine alongside gateway.run() so the send_message
    can actually be processed.  Fire-and-forget — never raises.
    """
    # No data yet means /me hasn't been fetched (or failed); nothing to check.
    if not config.superpos_permissions:
        return

    missing_required = [p for p in _REQUIRED_PERMISSIONS if not config.has_permission(p)]
    missing_optional = [p for p in _OPTIONAL_PERMISSIONS if not config.has_permission(p)]

    if missing_required:
        log.error("Agent missing required permissions: %s", missing_required)
    if missing_optional:
        log.warning("Agent missing optional permissions: %s", missing_optional)

    if not missing_required and not missing_optional:
        return
    if gateway is None or not config.telegram_chat_id:
        return

    lines = ["⚠️ Agent started with missing permissions:"]
    if missing_required:
        lines.append(f"  • Required (agent will malfunction): {', '.join(missing_required)}")
    if missing_optional:
        lines.append(f"  • Optional (some tasks may fail): {', '.join(missing_optional)}")
    lines.append("")
    lines.append("Grant them in the Superpos dashboard and restart the agent.")

    try:
        await gateway.send_message(config.telegram_chat_id, "\n".join(lines))
    except Exception:
        log.debug("Failed to send missing-permissions warning to Telegram", exc_info=True)


async def _monitor_disk(
    gateway: TelegramGateway | None,
    config: Config,
    *,
    path: str = "/home/agent/.claude",
    interval_seconds: int = 300,
    warn_threshold: float = 0.90,
    clear_threshold: float = 0.85,
) -> None:
    """Poll disk usage on the session-state volume and alert via Telegram.

    When a full disk truncates session_store.json / Claude CLI's per-chat
    JSONL files, the symptom surfaces much later as "agent lost context"
    — the operator sees a nonsense answer without any clue the underlying
    cause was disk pressure.  This task surfaces the warning early.

    Hysteresis via clear_threshold prevents alert flapping around the 90%
    boundary; the "recovered" message only fires after an active alert.
    """
    alerted = False
    while True:
        try:
            total, used, free = shutil.disk_usage(path)
            usage = used / total if total else 0.0

            if usage >= warn_threshold and not alerted:
                free_gb = free / (1024 ** 3)
                total_gb = total / (1024 ** 3)
                log.error(
                    "Disk nearly full: %.0f%% used (%.1fGB free of %.1fGB) at %s",
                    usage * 100, free_gb, total_gb, path,
                )
                if gateway is not None and config.telegram_chat_id:
                    msg = (
                        f"⚠️ Agent disk at {usage:.0%} "
                        f"({free_gb:.1f}GB free of {total_gb:.1f}GB).\n"
                        f"Session persistence may start failing — "
                        f"free up disk on the host before the agent loses context."
                    )
                    try:
                        await gateway.send_message(config.telegram_chat_id, msg)
                    except Exception:
                        log.debug("Failed to send disk warning", exc_info=True)
                alerted = True
            elif usage < clear_threshold and alerted:
                log.info("Disk usage recovered: %.0f%%", usage * 100)
                if gateway is not None and config.telegram_chat_id:
                    try:
                        await gateway.send_message(
                            config.telegram_chat_id,
                            f"✅ Agent disk recovered to {usage:.0%}.",
                        )
                    except Exception:
                        pass
                alerted = False
        except Exception:
            log.debug("Disk check failed", exc_info=True)

        await asyncio.sleep(interval_seconds)


async def _check_claude_auth() -> None:
    """Make a minimal SDK call to verify Claude credentials before starting."""
    log.info("Verifying Claude authentication...")
    try:
        async for _ in query(
            prompt="hi",
            options=ClaudeCodeOptions(max_turns=1, permission_mode="default"),
        ):
            pass  # consume all messages — breaking early corrupts anyio cancel scopes
        log.info("Claude authentication OK")
    except (ClaudeSDKError, Exception) as e:
        msg = _auth_error_message(str(e))
        if msg:
            print(msg, file=sys.stderr)
            sys.exit(1)
        raise


async def main() -> None:
    config = Config.from_env()

    # Prune orphaned worktrees from prior runs
    if config.claude_worktree_isolation and is_git_repo(config.claude_working_dir):
        try:
            await prune_worktrees(config.claude_working_dir)
        except Exception:
            log.warning("Failed to prune worktrees on startup", exc_info=True)

    # Verify Claude auth before starting anything else
    await _check_claude_auth()

    # Superpos client (optional)
    superpos: SuperposClient | None = None
    if config.superpos_enabled:
        superpos = SuperposClient(config)
        log.info("Superpos integration enabled (%s)", config.superpos_base_url)
        try:
            await superpos.update_status("online")
            log.info("Agent status set to online")
        except Exception:
            log.warning("Failed to set agent status to online", exc_info=True)

        # Overlay server-authoritative profile (hive_id, capabilities,
        # permissions) on top of env config.  Env stays as the fallback
        # so a /me outage doesn't ground the agent.
        me = await superpos.fetch_me()
        if me:
            if me.get("hive_id"):
                config.superpos_hive_id = me["hive_id"]
            caps = me.get("capabilities")
            if isinstance(caps, list) and caps:
                config.superpos_capabilities = [str(c) for c in caps]
            perms = me.get("permissions")
            if isinstance(perms, list):
                config.superpos_permissions = [str(p) for p in perms]
            log.info(
                "Agent profile: name=%r hive=%s capabilities=%s permissions=%d",
                me.get("name"), config.superpos_hive_id,
                config.superpos_capabilities, len(config.superpos_permissions),
            )
        else:
            log.warning(
                "Could not load /agents/me — falling back to env-configured "
                "hive_id=%s, capabilities=%s",
                config.superpos_hive_id, config.superpos_capabilities,
            )
    else:
        log.info("Superpos integration disabled (missing config)")

    # Telegram app + centralized gateway (optional)
    bot_app = None
    gateway: TelegramGateway | None = None
    if config.telegram_enabled:
        bot_app = build_telegram_app(config)
        gateway = TelegramGateway(bot_app.bot)
    else:
        log.info("Telegram disabled (no TELEGRAM_BOT_TOKEN)")

    # Fetch persona at startup
    persona: str | None = None
    if superpos:
        try:
            persona = await superpos.get_persona_assembled()
            if persona:
                log.info("Persona loaded (version from assembled endpoint)")
            else:
                log.info("No persona configured for this agent")
        except Exception:
            log.warning("Could not fetch persona at startup", exc_info=True)

    # Runtime-tunable overrides (model, effort) — env defaults, persisted JSON overlays
    runtime = RuntimeConfig.load(config)
    log.info("Runtime: model=%s, effort=%s", runtime.model, runtime.effort)

    # Executor
    executor = ClaudeExecutor(config, runtime, superpos, gateway, persona=persona)
    log.info("Executor: max_parallel=%d, worktree_isolation=%s",
             config.claude_max_parallel, config.claude_worktree_isolation)

    # Build task list
    tasks = [executor.run(), _monitor_disk(gateway, config)]
    if bot_app and gateway:
        tasks.append(run_telegram_bot(bot_app, executor, config, runtime))
        tasks.append(gateway.run())
    if superpos:
        tasks.append(run_superpos_poller(superpos, executor, config))
        tasks.append(_warn_missing_permissions(gateway, config))

    # An agent with neither Telegram nor Superpos has no way to receive
    # work — bail loudly instead of running an idle loop forever.
    if not bot_app and not superpos:
        log.error("Neither Telegram nor Superpos is configured — nothing to do")
        sys.exit(1)

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown(loop))

    # Auto-cleanup stale session data on startup (telegram-side housekeeping)
    if config.telegram_enabled:
        from .telegram_bot import _cleanup_stale_sessions
        counts = _cleanup_stale_sessions(max_age_hours=48)
        if counts["projects"] or counts["session_env"]:
            freed_mb = counts["bytes_freed"] / (1024 * 1024)
            log.info(
                "Startup cleanup: removed %d sessions, %d env snapshots (%.1fMB freed)",
                counts["projects"], counts["session_env"], freed_mb,
            )

    log.info("Starting %d tasks", len(tasks))
    try:
        await asyncio.gather(*tasks)
    finally:
        if superpos:
            try:
                await superpos.update_status("offline")
                log.info("Agent status set to offline")
            except Exception:
                log.debug("Failed to set agent status to offline (shutdown)")
            await superpos.close()


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Received shutdown signal")
    for task in asyncio.all_tasks(loop):
        task.cancel()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
