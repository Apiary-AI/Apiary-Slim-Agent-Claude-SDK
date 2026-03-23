"""Centralized Telegram API gateway.

All outgoing Telegram Bot API calls flow through a single processing loop,
eliminating lock contention and race conditions between concurrent streamers.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

log = logging.getLogger(__name__)


class Priority(enum.IntEnum):
    """Request priority — lower value = higher priority."""

    HIGH = 0  # sendMessage (initial sends), error messages
    NORMAL = 1  # sendChatAction
    LOW = 2  # editMessageText, deleteMessage, status updates


@dataclass(order=True)
class _TelegramRequest:
    """A queued Telegram API call with a Future for the result."""

    priority: int
    sequence: int  # tie-breaker for FIFO within same priority
    # Fields below excluded from ordering
    method: str = field(compare=False)
    kwargs: dict[str, Any] = field(compare=False)
    future: asyncio.Future = field(compare=False)
    supersede_key: str | None = field(default=None, compare=False)


class TelegramGateway:
    """Single point of contact for all outgoing Telegram API calls.

    Runs a processing loop that drains a priority queue at a controlled
    rate.  All callers interact via async methods that return results via
    Futures — callers await but never hold locks.
    """

    def __init__(
        self,
        bot: Bot,
        *,
        min_interval: float = 0.35,
        max_backoff: float = 120.0,
        circuit_threshold: int = 5,
    ) -> None:
        self._bot = bot
        self._min_interval = min_interval
        self._max_backoff = max_backoff
        self._circuit_threshold = circuit_threshold

        self._queue: asyncio.PriorityQueue[_TelegramRequest] = asyncio.PriorityQueue()
        self._pending_supersede: dict[str, _TelegramRequest] = {}
        self._last_call: float = 0.0
        self._backoff_until: float = 0.0
        self._consecutive_429s: int = 0
        self._sequence: int = 0

    # ── Public API ────────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        parse_mode: str | None = None,
        priority: Priority = Priority.HIGH,
    ) -> Any:
        """Queue a sendMessage call.  Returns the Message object or None."""
        return await self._submit(
            method="send_message",
            kwargs={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            priority=priority,
        )

    async def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        priority: Priority = Priority.LOW,
    ) -> Any:
        """Queue an editMessageText.  Supersedes prior edits to the same message."""
        return await self._submit(
            method="edit_message_text",
            kwargs={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            },
            priority=priority,
            supersede_key=f"edit:{chat_id}:{message_id}",
        )

    async def delete_message(
        self,
        chat_id: int | str,
        message_id: int,
        *,
        priority: Priority = Priority.LOW,
    ) -> Any:
        """Queue a deleteMessage call."""
        return await self._submit(
            method="delete_message",
            kwargs={"chat_id": chat_id, "message_id": message_id},
            priority=priority,
        )

    async def send_chat_action(
        self,
        chat_id: int | str,
        action: str,
        *,
        priority: Priority = Priority.NORMAL,
    ) -> Any:
        """Queue a sendChatAction call."""
        return await self._submit(
            method="send_chat_action",
            kwargs={"chat_id": chat_id, "action": action},
            priority=priority,
        )

    # ── Processing loop ───────────────────────────────────────────────

    async def run(self) -> None:
        """Main processing loop — run as a task in asyncio.gather()."""
        log.info(
            "TelegramGateway started (interval=%.2fs, circuit_threshold=%d)",
            self._min_interval,
            self._circuit_threshold,
        )
        try:
            while True:
                req = await self._queue.get()

                # Skip requests whose futures are already resolved (superseded)
                if req.future.done():
                    self._queue.task_done()
                    continue

                # Clean up supersede tracking
                if req.supersede_key:
                    self._pending_supersede.pop(req.supersede_key, None)

                # Wait for backoff
                now = time.monotonic()
                if self._backoff_until > now:
                    await asyncio.sleep(self._backoff_until - now)

                # Wait for minimum interval
                now = time.monotonic()
                wait = self._min_interval - (now - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)

                # Execute the API call
                self._last_call = time.monotonic()
                try:
                    bot_method = getattr(self._bot, req.method)
                    clean_kwargs = {k: v for k, v in req.kwargs.items() if v is not None}
                    result = await bot_method(**clean_kwargs)
                    self._consecutive_429s = 0
                    if not req.future.done():
                        req.future.set_result(result)
                except RetryAfter as e:
                    capped = min(e.retry_after, self._max_backoff)
                    self._backoff_until = max(
                        self._backoff_until, time.monotonic() + capped
                    )
                    self._consecutive_429s += 1
                    log.warning(
                        "Telegram 429 — backoff %.1fs (capped from %.1fs), consecutive=%d",
                        capped,
                        e.retry_after,
                        self._consecutive_429s,
                    )
                    if req.priority <= Priority.HIGH:
                        # Re-enqueue high-priority requests for retry
                        self._queue.put_nowait(req)
                    elif not req.future.done():
                        req.future.set_result(None)

                    if self._consecutive_429s >= self._circuit_threshold:
                        self._purge_droppable()
                except BadRequest as exc:
                    if not req.future.done():
                        req.future.set_exception(exc)
                except Exception as exc:
                    log.warning("Gateway API call failed: %s", exc)
                    if not req.future.done():
                        req.future.set_result(None)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info("TelegramGateway shutting down")
            raise

    # ── Internal ──────────────────────────────────────────────────────

    def _is_circuit_open(self) -> bool:
        if self._consecutive_429s >= self._circuit_threshold:
            return time.monotonic() < self._backoff_until
        return False

    async def _submit(
        self,
        method: str,
        kwargs: dict[str, Any],
        priority: Priority,
        supersede_key: str | None = None,
    ) -> Any:
        """Create a request, enqueue it, and await the result."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self._sequence += 1
        req = _TelegramRequest(
            priority=priority,
            sequence=self._sequence,
            method=method,
            kwargs=kwargs,
            future=future,
            supersede_key=supersede_key,
        )

        # Supersede older request for the same key
        if supersede_key and supersede_key in self._pending_supersede:
            old = self._pending_supersede[supersede_key]
            if not old.future.done():
                old.future.set_result(None)
        if supersede_key:
            self._pending_supersede[supersede_key] = req

        # Circuit breaker: drop droppable requests immediately
        if self._is_circuit_open() and priority >= Priority.LOW:
            future.set_result(None)
            return None

        self._queue.put_nowait(req)
        return await future

    def _purge_droppable(self) -> None:
        """Remove LOW/NORMAL priority items from the queue on circuit trip."""
        survivors: list[_TelegramRequest] = []
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                if req.priority <= Priority.HIGH and not req.future.done():
                    survivors.append(req)
                else:
                    if not req.future.done():
                        req.future.set_result(None)
                    self._queue.task_done()
            except asyncio.QueueEmpty:
                break
        for req in survivors:
            self._queue.put_nowait(req)
