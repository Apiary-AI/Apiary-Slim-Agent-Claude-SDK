"""Entry point: runs all daemons via asyncio.gather()."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import signal
import sys

from claude_code_sdk import ClaudeCodeOptions, ClaudeSDKError, query

from .apiary_client import ApiaryClient
from .apiary_poller import run_apiary_poller
from .claude_executor import ClaudeExecutor
from .config import Config
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
║      --entrypoint claude slim-apiary-agent                   ║
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
║      --entrypoint claude slim-apiary-agent                   ║
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

    # Apiary client (optional)
    apiary: ApiaryClient | None = None
    if config.apiary_enabled:
        apiary = ApiaryClient(config)
        log.info("Apiary integration enabled (%s)", config.apiary_base_url)
        try:
            await apiary.update_status("online")
            log.info("Agent status set to online")
        except Exception:
            log.warning("Failed to set agent status to online", exc_info=True)
    else:
        log.info("Apiary integration disabled (missing config)")

    if not config.telegram_enabled:
        log.error("TELEGRAM_BOT_TOKEN is required")
        sys.exit(1)

    # Telegram app + centralized gateway
    bot_app = build_telegram_app(config)
    bot = bot_app.bot
    gateway = TelegramGateway(bot)

    # Fetch persona at startup
    persona: str | None = None
    if apiary:
        try:
            persona = await apiary.get_persona_assembled()
            if persona:
                log.info("Persona loaded (version from assembled endpoint)")
            else:
                log.info("No persona configured for this agent")
        except Exception:
            log.warning("Could not fetch persona at startup", exc_info=True)

    # Executor
    executor = ClaudeExecutor(config, apiary, gateway, persona=persona)
    log.info("Executor: max_parallel=%d, worktree_isolation=%s",
             config.claude_max_parallel, config.claude_worktree_isolation)

    # Build task list
    tasks = [
        executor.run(),
        run_telegram_bot(bot_app, executor, config),
        gateway.run(),
    ]
    if apiary:
        tasks.append(run_apiary_poller(apiary, executor, config))

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown(loop))

    # Auto-cleanup stale session data on startup
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
        if apiary:
            try:
                await apiary.update_status("offline")
                log.info("Agent status set to offline")
            except Exception:
                log.debug("Failed to set agent status to offline (shutdown)")
            await apiary.close()


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Received shutdown signal")
    for task in asyncio.all_tasks(loop):
        task.cancel()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
