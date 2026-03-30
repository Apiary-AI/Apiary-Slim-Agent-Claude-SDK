"""Telegram bot daemon — receives messages and enqueues them for Claude."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import subprocess

import httpx
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

# Matches "PR #123", "#123", "pr #123", "PR#123", etc.
_PR_REF_RE = re.compile(r"(?:PR\s*)?#(\d+)", re.IGNORECASE)


async def _resolve_pr_branch(pr_number: int, repo_dir: str) -> str | None:
    """Resolve a PR number to its head branch via `gh pr view`."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "gh", "pr", "view", str(pr_number),
                "--json", "headRefName",
                "--jq", ".headRefName",
                "-R", ".",
            ],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            branch = result.stdout.strip()
            log.info("Resolved PR #%d → branch %r", pr_number, branch)
            return branch
        log.debug("gh pr view failed for #%d: %s", pr_number, result.stderr.strip())
    except Exception:
        log.debug("Failed to resolve PR #%d branch", pr_number, exc_info=True)
    return None


async def _transcribe_voice(ogg_path: str, api_key: str) -> str | None:
    """Transcribe a voice message using OpenAI Whisper API."""
    if not api_key:
        log.warning("Voice message received but OPENAI_API_KEY not set — skipping")
        return None
    try:
        async with httpx.AsyncClient() as client:
            with open(ogg_path, "rb") as f:
                resp = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": ("voice.ogg", f, "audio/ogg")},
                    data={"model": "whisper-1"},
                    timeout=30.0,
                )
            resp.raise_for_status()
            text = resp.json().get("text", "").strip()
            if text:
                log.info("Voice transcribed: %s...", text[:80])
            return text or None
    except Exception:
        log.warning("Voice transcription failed", exc_info=True)
        return None


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

    async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        executor.clear_session(update.effective_chat.id)
        await update.message.reply_text("🔄 Session cleared. Next message starts a fresh conversation.")

    async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        await update.message.reply_text("♻️ Restarting…")
        log.info("Restart requested by user %s — sending SIGTERM", update.effective_user.id)
        os.kill(os.getpid(), signal.SIGTERM)

    async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            log.warning(
                "Unauthorized user %s attempted access", update.effective_user
            )
            return
        if not update.message or not update.message.text:
            return

        text = update.message.text
        branch: str | None = None
        if text.startswith("--branch "):
            parts = text.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                text = parts[2] if len(parts) == 3 else ""

        # Auto-resolve branch from PR references when worktree isolation is on
        if not branch and config.claude_worktree_isolation:
            match = _PR_REF_RE.search(text)
            if match:
                pr_num = int(match.group(1))
                branch = await _resolve_pr_branch(pr_num, config.claude_working_dir)

        req = ExecutionRequest(
            prompt=text,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued telegram message from user %s (queue=%d, branch=%s)",
            update.effective_user.id,
            executor.pending,
            branch,
        )

    async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        if not update.message or not update.message.photo:
            return

        # Download largest resolution
        largest = update.message.photo[-1]
        tg_file = await largest.get_file()
        path = f"/tmp/tg_photo_{update.message.message_id}.jpg"
        await tg_file.download_to_drive(path)

        caption = update.message.caption or "Analyze this image."
        branch: str | None = None
        if caption.startswith("--branch "):
            parts = caption.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                caption = parts[2] if len(parts) == 3 else "Analyze this image."

        req = ExecutionRequest(
            prompt=caption,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
            image_paths=[path],
        )
        await executor.queue.put(req)
        log.info("Enqueued photo from user %s (queue=%d)", update.effective_user.id, executor.pending)

    async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        if not update.message or not update.message.voice:
            return

        voice = update.message.voice
        tg_file = await voice.get_file()
        ogg_path = f"/tmp/tg_voice_{update.message.message_id}.ogg"
        await tg_file.download_to_drive(ogg_path)

        transcript = await _transcribe_voice(ogg_path, config.openai_api_key)
        # Clean up OGG file after transcription
        try:
            os.unlink(ogg_path)
        except OSError:
            pass

        if not transcript:
            return

        req = ExecutionRequest(
            prompt=transcript,
            chat_id=update.effective_chat.id,
            source="telegram",
        )
        await executor.queue.put(req)
        log.info(
            "Enqueued voice message from user %s (queue=%d, transcript=%s...)",
            update.effective_user.id, executor.pending, transcript[:50],
        )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

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
