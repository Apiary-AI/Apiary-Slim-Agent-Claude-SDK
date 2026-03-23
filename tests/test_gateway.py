"""Tests for the centralized TelegramGateway."""

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from telegram.error import BadRequest, RetryAfter

from src.telegram_gateway import Priority, TelegramGateway


@pytest.fixture
def mock_bot():
    bot = AsyncMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    bot.edit_message_text = AsyncMock(return_value=True)
    bot.delete_message = AsyncMock(return_value=True)
    bot.send_chat_action = AsyncMock(return_value=True)
    return bot


@pytest.fixture
def gateway(mock_bot):
    return TelegramGateway(mock_bot, min_interval=0.0, max_backoff=1.0, circuit_threshold=3)


# ── Basic request flow ─────────────────────────────────────────────


async def test_send_message_flows_through(gateway, mock_bot):
    """A send_message call should reach the bot and return the result."""
    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.send_message("123", "hello"),
            timeout=2.0,
        )
        assert result.message_id == 42
        mock_bot.send_message.assert_called_once_with(chat_id="123", text="hello")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_edit_message_flows_through(gateway, mock_bot):
    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.edit_message_text("123", 42, "updated"),
            timeout=2.0,
        )
        assert result is True
        mock_bot.edit_message_text.assert_called_once_with(
            chat_id="123", message_id=42, text="updated"
        )
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_delete_message_flows_through(gateway, mock_bot):
    loop_task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(
            gateway.delete_message("123", 42),
            timeout=2.0,
        )
        mock_bot.delete_message.assert_called_once_with(chat_id="123", message_id=42)
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_send_chat_action_flows_through(gateway, mock_bot):
    loop_task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(
            gateway.send_chat_action("123", "typing"),
            timeout=2.0,
        )
        mock_bot.send_chat_action.assert_called_once_with(chat_id="123", action="typing")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_none_kwargs_are_filtered(gateway, mock_bot):
    """parse_mode=None should not be passed to the bot method."""
    loop_task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(
            gateway.send_message("123", "hi", parse_mode=None),
            timeout=2.0,
        )
        mock_bot.send_message.assert_called_once_with(chat_id="123", text="hi")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Rate limiting ──────────────────────────────────────────────────


async def test_rate_limiting_enforces_interval(mock_bot):
    """Two calls should be spaced by at least min_interval."""
    gw = TelegramGateway(mock_bot, min_interval=0.1, max_backoff=1.0)
    loop_task = asyncio.create_task(gw.run())
    try:
        t0 = time.monotonic()
        await asyncio.wait_for(gw.send_message("1", "a"), timeout=2.0)
        await asyncio.wait_for(gw.send_message("1", "b"), timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.1
        assert mock_bot.send_message.call_count == 2
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── 429 handling ───────────────────────────────────────────────────


async def test_429_retries_high_priority(gateway, mock_bot):
    """HIGH priority requests should be retried after 429."""
    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RetryAfter(0.1)
        return MagicMock(message_id=99)

    mock_bot.send_message = AsyncMock(side_effect=side_effect)

    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.send_message("123", "retry me"),
            timeout=3.0,
        )
        assert result.message_id == 99
        assert call_count == 2
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_429_drops_low_priority(gateway, mock_bot):
    """LOW priority requests should be dropped (resolved with None) on 429."""
    mock_bot.edit_message_text = AsyncMock(side_effect=RetryAfter(0.5))

    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.edit_message_text("123", 42, "drop me"),
            timeout=3.0,
        )
        assert result is None
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


async def test_429_resets_on_success(gateway, mock_bot):
    """Consecutive 429 counter should reset after a successful call."""
    call_count = 0

    async def side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RetryAfter(0.05)
        return MagicMock(message_id=1)

    mock_bot.send_message = AsyncMock(side_effect=side_effect)

    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.send_message("123", "eventually works"),
            timeout=3.0,
        )
        assert result is not None
        assert gateway._consecutive_429s == 0
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Circuit breaker ────────────────────────────────────────────────


async def test_circuit_breaker_drops_low_priority_at_enqueue(mock_bot):
    """When circuit is open, LOW priority requests are dropped immediately."""
    gw = TelegramGateway(mock_bot, min_interval=0.0, max_backoff=10.0, circuit_threshold=2)
    # Simulate circuit open
    gw._consecutive_429s = 2
    gw._backoff_until = time.monotonic() + 10.0

    result = await gw.edit_message_text("123", 42, "should be dropped")
    assert result is None
    # Nothing should have been enqueued
    assert gw._queue.empty()


async def test_circuit_breaker_allows_high_priority(mock_bot):
    """HIGH priority requests should go through even when circuit is open."""
    gw = TelegramGateway(mock_bot, min_interval=0.0, max_backoff=0.1, circuit_threshold=2)
    gw._consecutive_429s = 2
    gw._backoff_until = time.monotonic() + 0.1

    loop_task = asyncio.create_task(gw.run())
    try:
        result = await asyncio.wait_for(
            gw.send_message("123", "important"),
            timeout=2.0,
        )
        assert result is not None
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Superseding ────────────────────────────────────────────────────


async def test_superseding_cancels_older_edit(mock_bot):
    """Newer edit to the same message should supersede the older one."""
    gw = TelegramGateway(mock_bot, min_interval=0.0, max_backoff=1.0)

    # Submit two edits to the same message without starting the loop
    fut1 = asyncio.get_event_loop().create_future()
    fut2 = asyncio.get_event_loop().create_future()

    # First edit
    task1 = asyncio.create_task(gw.edit_message_text("123", 42, "old text"))
    await asyncio.sleep(0)  # let it enqueue

    # Second edit to same message — should supersede
    task2 = asyncio.create_task(gw.edit_message_text("123", 42, "new text"))
    await asyncio.sleep(0)

    # Start processing
    loop_task = asyncio.create_task(gw.run())
    try:
        result1 = await asyncio.wait_for(task1, timeout=2.0)
        result2 = await asyncio.wait_for(task2, timeout=2.0)

        # First should have been superseded (resolved None)
        assert result1 is None
        # Second should have executed
        assert result2 is True
        # Bot should have been called only once (for the second edit)
        mock_bot.edit_message_text.assert_called_once_with(
            chat_id="123", message_id=42, text="new text"
        )
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── Priority ordering ──────────────────────────────────────────────


async def test_high_priority_executes_before_low(mock_bot):
    """HIGH priority send_message should execute before LOW priority edit."""
    gw = TelegramGateway(mock_bot, min_interval=0.0, max_backoff=1.0)
    call_order = []

    async def track_send(**kwargs):
        call_order.append("send")
        return MagicMock(message_id=1)

    async def track_edit(**kwargs):
        call_order.append("edit")
        return True

    mock_bot.send_message = AsyncMock(side_effect=track_send)
    mock_bot.edit_message_text = AsyncMock(side_effect=track_edit)

    # Enqueue LOW first, then HIGH — without starting loop
    edit_task = asyncio.create_task(
        gw.edit_message_text("123", 42, "edit")
    )
    await asyncio.sleep(0)
    send_task = asyncio.create_task(
        gw.send_message("123", "send")
    )
    await asyncio.sleep(0)

    loop_task = asyncio.create_task(gw.run())
    try:
        await asyncio.wait_for(send_task, timeout=2.0)
        await asyncio.wait_for(edit_task, timeout=2.0)
        # HIGH (send) should have executed before LOW (edit)
        assert call_order == ["send", "edit"]
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── BadRequest propagation ─────────────────────────────────────────


async def test_bad_request_propagated_to_caller(gateway, mock_bot):
    """BadRequest exceptions should be raised in the caller."""
    mock_bot.send_message = AsyncMock(
        side_effect=BadRequest("message too long")
    )
    loop_task = asyncio.create_task(gateway.run())
    try:
        with pytest.raises(BadRequest, match="message too long"):
            await asyncio.wait_for(
                gateway.send_message("123", "x" * 10000),
                timeout=2.0,
            )
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass


# ── General error handling ─────────────────────────────────────────


async def test_generic_error_returns_none(gateway, mock_bot):
    """Unknown errors should resolve with None, not crash the gateway."""
    mock_bot.send_message = AsyncMock(side_effect=ConnectionError("network down"))

    loop_task = asyncio.create_task(gateway.run())
    try:
        result = await asyncio.wait_for(
            gateway.send_message("123", "hello"),
            timeout=2.0,
        )
        assert result is None
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
