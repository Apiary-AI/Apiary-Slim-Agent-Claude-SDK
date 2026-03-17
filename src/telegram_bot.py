"""Telegram bot daemon — receives messages and enqueues them for Claude."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .claude_executor import ClaudeExecutor, ExecutionRequest
from .config import Config

log = logging.getLogger(__name__)


def build_telegram_app(config: Config) -> Application:
    """Build a python-telegram-bot Application (do NOT call run_polling)."""
    return Application.builder().token(config.telegram_bot_token).build()


async def run_telegram_bot(
    app: Application,
    executor: ClaudeExecutor,
    config: Config,
) -> None:
    """Start the bot using non-blocking polling (compatible with asyncio.gather)."""

    allowed = set(config.telegram_allowed_users)

    def is_allowed(user_id: int) -> bool:
        return not allowed or user_id in allowed

    async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            "👋 Hi! Send me any message and I'll process it with Claude."
        )

    async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text(
            f"📊 Queue depth: {executor.pending}"
        )

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            log.warning(
                "Unauthorized user %s attempted access", update.effective_user
            )
            return
        if not update.message or not update.message.text:
            return

        req = ExecutionRequest(
            prompt=update.message.text,
            chat_id=update.effective_chat.id,
            source="telegram",
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued telegram message from user %s (queue=%d)",
            update.effective_user.id,
            executor.pending,
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Non-blocking start: initialize + start + begin polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    log.info("Telegram bot started polling")

    # Keep alive until cancelled
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        log.info("Telegram bot shutting down")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
