"""Telegram forum-topic support: per-topic sessions, streaming, and /stop.

Core (superpos-agent-core) extracts ``thread_id`` from incoming updates
and keys conversations by ``ExecutionRequest.chat_key`` ("chat:thread"
for topics, plain "chat" otherwise).  These tests pin the executor's side
of the contract: the streamer must target the topic the request came
from, and all per-conversation state (session store, /stop tracking)
must use the composite key so topics stay independent.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from superpos_agent_core.executor import ExecutionRequest


def _telegram_req(thread_id=None, prompt="hello"):
    return ExecutionRequest(
        prompt=prompt, chat_id=123, source="telegram", thread_id=thread_id,
    )


# --- Streamer targets the originating topic ---

async def test_streamer_created_with_request_thread_id(executor, mock_gateway):
    req = _telegram_req(thread_id=42)

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None, progress_event=None):
        return None

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        MockStreamer.return_value.finish = AsyncMock()
        await executor.queue.put(req)
        await executor.queue.get()
        await executor._run_one(req)

    MockStreamer.assert_called_once_with(mock_gateway, 123, thread_id=42)


async def test_streamer_thread_id_none_outside_topics(executor, mock_gateway):
    req = _telegram_req()

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None, progress_event=None):
        return None

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("superpos_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        MockStreamer.return_value.finish = AsyncMock()
        await executor.queue.put(req)
        await executor.queue.get()
        await executor._run_one(req)

    MockStreamer.assert_called_once_with(mock_gateway, 123, thread_id=None)


# --- Sessions are independent per topic ---

def test_resume_target_keyed_per_topic(executor):
    executor._sessions.set_with_version("123:42", "sid-topic", None)
    executor._sessions.set_with_version("123", "sid-plain", None)

    resume_topic, _ = executor._resolve_resume_target(_telegram_req(thread_id=42))
    resume_plain, _ = executor._resolve_resume_target(_telegram_req())
    resume_other, _ = executor._resolve_resume_target(_telegram_req(thread_id=43))

    assert resume_topic == "sid-topic"
    assert resume_plain == "sid-plain"
    assert resume_other is None, "a fresh topic must not inherit another topic's session"


def test_peek_effective_branch_keyed_per_topic(executor):
    executor._sessions.set_with_version(
        "123:42", "sid-topic", None, branch="feat/topic-work",
    )

    assert executor._peek_effective_branch(_telegram_req(thread_id=42)) == "feat/topic-work"
    assert executor._peek_effective_branch(_telegram_req()) is None


def test_clear_session_scoped_to_topic(executor):
    executor._sessions.set_with_version("123:42", "sid-topic", None)
    executor._sessions.set_with_version("123", "sid-plain", None)

    executor.clear_session("123:42")

    assert executor._sessions.get("123:42") is None
    assert executor._sessions.get("123") == "sid-plain"


# --- /stop cancels only the calling topic's work ---

async def test_cancel_chat_scoped_to_topic(executor):
    async def forever():
        await asyncio.sleep(30)

    topic_task = asyncio.create_task(forever())
    plain_task = asyncio.create_task(forever())
    executor._track_chat_task(_telegram_req(thread_id=42).chat_key, topic_task)
    executor._track_chat_task(_telegram_req().chat_key, plain_task)
    try:
        assert executor.cancel_chat("123:42") == 1
        await asyncio.sleep(0)  # let the cancellation propagate
        assert topic_task.cancelled()
        assert not plain_task.done(), "plain-chat work must survive a topic's /stop"
        assert executor.cancel_chat("123") == 1
    finally:
        for t in (topic_task, plain_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
