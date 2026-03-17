"""Streams Claude output to Telegram by editing messages in real-time."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from telegram import Bot
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, RetryAfter

log = logging.getLogger(__name__)

# Telegram message text limit (leave margin for formatting)
MAX_MSG_LEN = 4000
MIN_EDIT_INTERVAL = 1.0  # seconds between edits

# ── Human-readable tool descriptions ─────────────────────────────────

_TOOL_LABELS: dict[str, str] = {
    "Bash": "Running command",
    "Read": "Reading",
    "Write": "Writing",
    "Edit": "Editing",
    "Glob": "Searching files",
    "Grep": "Searching code",
    "WebSearch": "Searching the web",
    "WebFetch": "Fetching page",
    "Agent": "Running sub-agent",
    "NotebookEdit": "Editing notebook",
}


def _humanize_tool(tool_name: str, tool_input: Any) -> str:
    """Create a human-readable one-liner for a tool invocation."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    label = _TOOL_LABELS.get(tool_name, f"Using {tool_name}")

    detail = ""
    if tool_name == "Bash":
        cmd = inp.get("command", "")
        detail = cmd.split("&&")[0].split("|")[0].strip()
    elif tool_name in ("Read", "Write", "Edit"):
        path = inp.get("file_path", "")
        if path:
            detail = path.rsplit("/", 1)[-1]
    elif tool_name == "Glob":
        detail = inp.get("pattern", "")
    elif tool_name == "Grep":
        detail = inp.get("pattern", "")
    elif tool_name == "WebSearch":
        detail = inp.get("query", "")
    elif tool_name == "WebFetch":
        detail = inp.get("url", "")
    elif tool_name == "Agent":
        detail = inp.get("description", inp.get("prompt", ""))

    if detail:
        if len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{label}: {detail}"
    return label


def md_to_telegram(text: str) -> str:
    """Convert GitHub-flavored Markdown to Telegram MarkdownV2."""
    # Preserve code blocks first (don't touch content inside them)
    code_blocks: list[str] = []

    def _save_code_block(m: re.Match) -> str:
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    # Save fenced code blocks
    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    # Save inline code
    inline_codes: list[str] = []

    def _save_inline(m: re.Match) -> str:
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`[^`]+`", _save_inline, text)

    # Headings: ## Text → *Text*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Bold: **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Escape special MarkdownV2 characters (except those we use for formatting)
    # Must escape: _ [ ] ( ) ~ > + - = | { } . !
    def _escape(t: str) -> str:
        return re.sub(r"([_\[\]()~>+\-=|{}.!\\#])", r"\\\1", t)

    # Split by our placeholders, escape only non-code parts
    parts = re.split(r"(\x00(?:CODEBLOCK|INLINE)\d+\x00)", text)
    result = []
    for part in parts:
        if part.startswith("\x00CODEBLOCK"):
            idx = int(part.strip("\x00").replace("CODEBLOCK", ""))
            result.append(code_blocks[idx])
        elif part.startswith("\x00INLINE"):
            idx = int(part.strip("\x00").replace("INLINE", ""))
            result.append(inline_codes[idx])
        else:
            # Escape special chars but preserve * for bold/headings
            part = re.sub(r"([_\[\]()~>+\-=|{}.!\\#])", r"\\\1", part)
            result.append(part)

    return "".join(result)


class TelegramStreamer:
    """Accumulates text and pushes it to Telegram via message editing."""

    def __init__(self, bot: Bot, chat_id: int | str) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._messages: list[int] = []  # sent message IDs
        self._buffer = ""
        self._last_edit: float = 0.0
        self._current_msg_id: int | None = None
        self._status_msg_id: int | None = None
        self._tool_count: int = 0
        self._status_description: str = ""
        self._status_started: float = 0.0
        self._status_ticker: asyncio.Task | None = None

    async def start(self) -> None:
        await self._bot.send_chat_action(chat_id=self._chat_id, action=ChatAction.TYPING)
        self._current_msg_id = None
        self._buffer = ""
        self._last_edit = time.monotonic()

    async def append(self, text: str) -> None:
        """Append text to the stream. Edits the current message if enough time passed."""
        if not text:
            return
        self._buffer += text

        try:
            # Send first message once we have content
            if self._current_msg_id is None:
                msg = await self._send_formatted(self._buffer[:4096])
                self._current_msg_id = msg.message_id
                self._messages.append(msg.message_id)
                self._last_edit = time.monotonic()
                return

            # If buffer exceeds limit, finalize current message and start a new one
            if len(self._buffer) > MAX_MSG_LEN:
                await self._finalize_current()
                return

            # Rate-limit edits
            now = time.monotonic()
            if now - self._last_edit >= MIN_EDIT_INTERVAL:
                await self._edit_current()
        except Exception:
            log.warning("Telegram network error in append (text buffered)", exc_info=True)

    async def finish(self) -> None:
        """Finalize the stream: flush remaining buffer and clean up status."""
        try:
            await self._delete_status()

            if not self._buffer:
                return

            # If no message sent yet, send one now
            if self._current_msg_id is None:
                msg = await self._send_formatted(self._buffer[:4096])
                self._current_msg_id = msg.message_id
                self._messages.append(msg.message_id)
                return

            # Handle overflow on final flush
            if len(self._buffer) > MAX_MSG_LEN:
                await self._finalize_current()

            await self._edit_current()
        except Exception:
            log.warning("Telegram network error in finish", exc_info=True)

    async def send_tool_notification(self, tool_name: str, tool_input: Any) -> None:
        """Update a separate status message with current tool activity."""
        self._tool_count += 1
        self._status_description = _humanize_tool(tool_name, tool_input)
        self._status_started = time.monotonic()

        # Cancel previous ticker if running
        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()

        await self._update_status_text()

        # Start ticker to update elapsed time every 10s
        self._status_ticker = asyncio.create_task(self._run_status_ticker())

    async def _run_status_ticker(self) -> None:
        """Periodically update the status message with elapsed time."""
        try:
            while True:
                await asyncio.sleep(10)
                await self._update_status_text()
        except asyncio.CancelledError:
            pass

    def _format_elapsed(self) -> str:
        elapsed = int(time.monotonic() - self._status_started)
        if elapsed < 60:
            return f"{elapsed}s"
        return f"{elapsed // 60}m {elapsed % 60:02d}s"

    async def _update_status_text(self) -> None:
        elapsed = self._format_elapsed()
        status_text = f"⏳ {self._status_description} ({elapsed})"
        try:
            if self._status_msg_id is None:
                msg = await self._bot.send_message(
                    chat_id=self._chat_id, text=status_text,
                )
                self._status_msg_id = msg.message_id
            else:
                await self._bot.edit_message_text(
                    chat_id=self._chat_id,
                    message_id=self._status_msg_id,
                    text=status_text,
                )
        except (BadRequest, RetryAfter):
            pass  # Non-critical — skip if update fails

    async def error(self, error_text: str) -> None:
        """Send an error message (fire-and-forget — must never crash)."""
        try:
            await self._bot.send_message(
                chat_id=self._chat_id, text=f"❌ {error_text}"
            )
        except Exception:
            log.warning("Failed to send error message to Telegram", exc_info=True)

    # ── Internal ──────────────────────────────────────────────────────

    async def _send_formatted(self, text: str) -> Any:
        """Send a new message with MarkdownV2, falling back to plain text."""
        try:
            return await self._bot.send_message(
                chat_id=self._chat_id,
                text=md_to_telegram(text),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except BadRequest:
            return await self._bot.send_message(
                chat_id=self._chat_id, text=text,
            )

    async def _delete_status(self) -> None:
        """Cancel the ticker and delete the ephemeral status message."""
        if self._status_ticker and not self._status_ticker.done():
            self._status_ticker.cancel()
            self._status_ticker = None
        if self._status_msg_id is not None:
            try:
                await self._bot.delete_message(
                    chat_id=self._chat_id,
                    message_id=self._status_msg_id,
                )
            except BadRequest:
                pass
            self._status_msg_id = None

    async def _edit_current(self) -> None:
        if not self._current_msg_id or not self._buffer:
            return
        try:
            formatted = md_to_telegram(self._buffer[:4096])
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._current_msg_id,
                text=formatted,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            self._last_edit = time.monotonic()
        except RetryAfter as e:
            log.warning("Telegram rate limit, waiting %s seconds", e.retry_after)
            await asyncio.sleep(e.retry_after)
            await self._edit_current()
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                # Fallback: send without formatting if MarkdownV2 fails
                log.warning("Markdown parse failed, falling back to plain text: %s", e)
                try:
                    await self._bot.edit_message_text(
                        chat_id=self._chat_id,
                        message_id=self._current_msg_id,
                        text=self._buffer[:4096],
                    )
                    self._last_edit = time.monotonic()
                except BadRequest:
                    pass

    async def _finalize_current(self) -> None:
        """Finalize the current message at MAX_MSG_LEN and start a new one."""
        finalize_text = self._buffer[:MAX_MSG_LEN]
        overflow = self._buffer[MAX_MSG_LEN:]

        self._buffer = finalize_text
        await self._edit_current()

        # Start new message with overflow
        msg = await self._send_formatted(overflow or "...")
        self._current_msg_id = msg.message_id
        self._messages.append(msg.message_id)
        self._buffer = overflow or ""
        self._last_edit = time.monotonic()
