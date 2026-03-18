import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.claude_executor import ClaudeExecutor, ExecutionRequest


# --- Dedup method unit tests (pure sync logic) ---

def test_has_task_initially_false(executor):
    assert not executor.has_apiary_task("abc")


def test_add_then_has(executor):
    executor.add_apiary_task("abc")
    assert executor.has_apiary_task("abc")


def test_remove_clears_task(executor):
    executor.add_apiary_task("abc")
    executor.remove_apiary_task("abc")
    assert not executor.has_apiary_task("abc")


def test_remove_nonexistent_is_safe(executor):
    executor.remove_apiary_task("nonexistent")  # must not raise


# --- _report_progress: 409 sets event, other errors don't ---

async def test_report_progress_409_sets_event(executor, mock_apiary):
    mock_response = Mock()
    mock_response.status_code = 409
    mock_apiary.update_progress.side_effect = httpx.HTTPStatusError(
        "conflict", request=Mock(), response=mock_response
    )
    claim_expired = asyncio.Event()
    await executor._report_progress("task-1", claim_expired, interval=0.01)
    assert claim_expired.is_set()


async def test_report_progress_500_does_not_set_event(executor, mock_apiary):
    mock_response = Mock()
    mock_response.status_code = 500
    mock_apiary.update_progress.side_effect = [
        httpx.HTTPStatusError("server error", request=Mock(), response=mock_response),
        asyncio.CancelledError(),  # stop the loop on second iteration
    ]
    claim_expired = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await executor._report_progress("task-1", claim_expired, interval=0.01)
    assert not claim_expired.is_set()


async def test_report_progress_generic_exception_does_not_set_event(executor, mock_apiary):
    mock_apiary.update_progress.side_effect = [
        Exception("network error"),
        asyncio.CancelledError(),
    ]
    claim_expired = asyncio.Event()
    with pytest.raises(asyncio.CancelledError):
        await executor._report_progress("task-1", claim_expired, interval=0.01)
    assert not claim_expired.is_set()


# --- Claim expiry removes task from in-flight set ---

async def test_execute_removes_task_after_claim_expiry(executor):
    executor.add_apiary_task("task-x")

    async def fake_report_progress(task_id, claim_expired, interval=30):
        claim_expired.set()

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(10)  # blocks until cancelled

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="apiary", apiary_task_id="task-x"
    )

    with patch.object(executor, "_report_progress", fake_report_progress), \
         patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        await asyncio.wait_for(executor._execute(req), timeout=2.0)

    assert not executor.has_apiary_task("task-x")
