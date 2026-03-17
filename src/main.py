"""Entry point: runs all daemons via asyncio.gather()."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .apiary_client import ApiaryClient
from .apiary_poller import run_apiary_poller
from .claude_executor import ClaudeExecutor
from .config import Config
from .telegram_bot import build_telegram_app, run_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


async def main() -> None:
    config = Config.from_env()

    # Apiary client (optional)
    apiary: ApiaryClient | None = None
    if config.apiary_enabled:
        apiary = ApiaryClient(config)
        log.info("Apiary integration enabled (%s)", config.apiary_base_url)
    else:
        log.info("Apiary integration disabled (missing config)")

    if not config.telegram_enabled:
        log.error("TELEGRAM_BOT_TOKEN is required")
        sys.exit(1)

    # Telegram app
    bot_app = build_telegram_app(config)
    bot = bot_app.bot

    # Executor
    executor = ClaudeExecutor(config, apiary, bot)

    # Build task list
    tasks = [
        executor.run(),
        run_telegram_bot(bot_app, executor, config),
    ]
    if apiary:
        tasks.append(run_apiary_poller(apiary, executor, config))

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: _shutdown(loop))

    log.info("Starting %d tasks", len(tasks))
    await asyncio.gather(*tasks)


def _shutdown(loop: asyncio.AbstractEventLoop) -> None:
    log.info("Received shutdown signal")
    for task in asyncio.all_tasks(loop):
        task.cancel()


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
