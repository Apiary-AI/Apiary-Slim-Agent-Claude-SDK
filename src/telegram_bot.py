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
from .runtime_config import RuntimeConfig

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


def _cleanup_stale_sessions(
    max_age_hours: int = 24,
    preserve_session_ids: set[str] | None = None,
) -> dict[str, int]:
    """Remove old Claude session data to reclaim disk space.

    ``preserve_session_ids`` — session IDs that must NOT be deleted regardless
    of age. The SessionStore's active mappings (chat_id → session_id) belong
    here: if the user has been idle for >max_age_hours, the resume target
    would otherwise be deleted and the next message silently starts a fresh
    Claude session (perceived as "agent lost the conversation on restart").
    """
    import shutil
    import time

    preserve = preserve_session_ids or set()
    counts = {"projects": 0, "session_env": 0, "bytes_freed": 0}
    cutoff = time.time() - (max_age_hours * 3600)
    claude_dir = os.path.join(os.environ.get("HOME", "/tmp"), ".claude")

    def _session_id_from_name(name: str) -> str:
        # Both `<sid>` (dir) and `<sid>.jsonl` (transcript) live in projects/.
        return name[:-6] if name.endswith(".jsonl") else name

    # Clean old project sessions
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

    # Clean old session-env dirs
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
                    shutil.rmtree(path)
                    counts["session_env"] += 1
            except OSError:
                pass

    return counts


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
    runtime: RuntimeConfig,
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

    async def cmd_cleanup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        preserve = executor._sessions.active_session_ids()
        counts = await asyncio.to_thread(_cleanup_stale_sessions, 24, preserve)
        freed_mb = counts["bytes_freed"] / (1024 * 1024)
        await update.message.reply_text(
            f"🧹 Cleaned up:\n"
            f"  Sessions: {counts['projects']}\n"
            f"  Env snapshots: {counts['session_env']}\n"
            f"  Freed: {freed_mb:.1f}MB"
        )

    async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        args = ctx.args or []
        if not args:
            await update.message.reply_text(
                f"Current model: `{runtime.model}`\n\n"
                f"Usage: `/model <id>` or `/model list`",
                parse_mode="Markdown",
            )
            return
        if args[0] == "list":
            listing = "\n".join(f"- `{m}`" for m in RuntimeConfig.KNOWN_MODELS)
            await update.message.reply_text(
                f"Known models:\n{listing}\n\n"
                f"Any provider model id is accepted — known list is a hint.",
                parse_mode="Markdown",
            )
            return
        try:
            runtime.set_model(args[0])
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        log.info("Model changed to %s by user %s", runtime.model, update.effective_user.id)
        await update.message.reply_text(
            f"✅ Model set to `{runtime.model}` (takes effect on next task).",
            parse_mode="Markdown",
        )

    async def cmd_effort(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        args = ctx.args or []
        if not args:
            levels = ", ".join(RuntimeConfig.EFFORT_LEVELS)
            await update.message.reply_text(
                f"Current effort: `{runtime.effort}`\n\n"
                f"Usage: `/effort <{levels}>`",
                parse_mode="Markdown",
            )
            return
        try:
            runtime.set_effort(args[0])
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        log.info("Effort changed to %s by user %s", runtime.effort, update.effective_user.id)
        await update.message.reply_text(
            f"✅ Effort set to `{runtime.effort}` (takes effect on next task).",
            parse_mode="Markdown",
        )

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

    async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_user or not is_allowed(update.effective_user.id):
            return
        if not update.message or not update.message.document:
            return

        doc = update.message.document
        tg_file = await doc.get_file()
        filename = doc.file_name or f"file_{update.message.message_id}"
        path = f"/tmp/tg_doc_{update.message.message_id}_{filename}"
        await tg_file.download_to_drive(path)

        caption = update.message.caption or f"I sent you a file: {filename}. Work with it as needed."
        branch: str | None = None
        if caption.startswith("--branch "):
            parts = caption.split(" ", 2)
            if len(parts) >= 2:
                branch = parts[1]
                caption = parts[2] if len(parts) == 3 else f"I sent you a file: {filename}. Work with it as needed."

        # Tell Claude where the file is so it can read/use it
        prompt = (
            f"The user sent a file. It has been saved to: {path}\n"
            f"File name: {filename}\n\n{caption}"
        )

        req = ExecutionRequest(
            prompt=prompt,
            chat_id=update.effective_chat.id,
            source="telegram",
            branch=branch,
        )
        await executor.queue.put(req)
        log.info("Enqueued document '%s' from user %s (queue=%d)", filename, update.effective_user.id, executor.pending)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

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
