"""Tests for the AskUserQuestion-over-Telegram SDK MCP tool.

The executor registers an in-process SDK MCP tool (``ask_user``) that shadows
the native ``AskUserQuestion``.  Its handler reads the live run's
chat_id/thread_id from a contextvar, calls agent-core's
``ask_user_question`` (rendering an inline keyboard in Telegram), and returns
the user's selection as the tool result.  These tests exercise the handler in
isolation by mocking ``ask_user_question`` — a real Telegram round-trip can't
run in CI.
"""

import json

import pytest

from superpos_agent_core import AskAlreadyPending

import superpos_agent_claude.claude_executor as ce
from superpos_agent_claude.claude_executor import (
    _ASK_MCP_SERVER,
    _ASK_MCP_TOOL,
    _ASK_MCP_TOOL_FQN,
    _AskContext,
    _ask_context_var,
)


@pytest.fixture
def handler(executor):
    """Return the raw async handler callable for the ask_user tool.

    We rebuild the server in a way that exposes the SdkMcpTool list so we can
    call the handler directly. create_sdk_mcp_server stores tools internally,
    so instead we capture the SdkMcpTool by re-invoking _build_ask_mcp_server
    with a patched create_sdk_mcp_server.
    """
    captured = {}

    real = ce.create_sdk_mcp_server

    def _capture(name, version="1.0.0", tools=None):
        captured["tools"] = tools
        return real(name, version=version, tools=tools)

    ce.create_sdk_mcp_server = _capture
    try:
        executor._build_ask_mcp_server()
    finally:
        ce.create_sdk_mcp_server = real

    tools = captured["tools"]
    assert len(tools) == 1
    assert tools[0].name == _ASK_MCP_TOOL
    return tools[0].handler


def _set_ctx(chat_id=999, thread_id=7, pause=None, resume=None):
    calls = {"pause": 0, "resume": 0}

    def _pause():
        calls["pause"] += 1

    def _resume():
        calls["resume"] += 1

    ctx = _AskContext(
        chat_id=chat_id,
        thread_id=thread_id,
        pause=pause or _pause,
        resume=resume or _resume,
    )
    token = _ask_context_var.set(ctx)
    return ctx, calls, token


QUESTIONS = [
    {
        "header": "Deploy?",
        "question": "Should I deploy to prod?",
        "multiSelect": False,
        "options": [
            {"label": "Yes", "description": "ship it"},
            {"label": "No", "description": "hold"},
        ],
    }
]


@pytest.mark.asyncio
async def test_handler_maps_input_and_returns_answer(handler, monkeypatch):
    """Handler forwards chat_id/thread_id/gateway/questions to
    ask_user_question and returns its result as JSON tool content."""
    seen = {}

    async def fake_ask(chat_id, thread_id, questions, *, gateway, timeout, **kw):
        seen.update(
            chat_id=chat_id, thread_id=thread_id, questions=questions,
            gateway=gateway, timeout=timeout,
        )
        return {
            "answers": [
                {"header": "Deploy?", "question": "Should I deploy to prod?",
                 "selected": ["Yes"]}
            ],
            "timed_out": False,
        }

    monkeypatch.setattr(ce, "ask_user_question", fake_ask)

    ctx, calls, token = _set_ctx(chat_id=42, thread_id=3)
    try:
        result = await handler({"questions": QUESTIONS})
    finally:
        _ask_context_var.reset(token)

    assert seen["chat_id"] == 42
    assert seen["thread_id"] == 3
    assert seen["questions"] == QUESTIONS
    assert seen["gateway"] is not None  # gateway threaded through (see dedicated test)
    assert seen["timeout"] == 600.0

    # tool result is non-error JSON with the answer
    assert result.get("is_error") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["answers"][0]["selected"] == ["Yes"]
    assert payload["timed_out"] is False

    # watchdog paused for the duration then resumed
    assert calls["pause"] == 1
    assert calls["resume"] == 1


@pytest.mark.asyncio
async def test_handler_passes_executor_gateway(executor, monkeypatch):
    """The gateway passed to ask_user_question is the executor's gateway."""
    seen = {}

    async def fake_ask(chat_id, thread_id, questions, *, gateway, timeout, **kw):
        seen["gateway"] = gateway
        return {"answers": [], "timed_out": False}

    monkeypatch.setattr(ce, "ask_user_question", fake_ask)

    # rebuild handler bound to this executor
    captured = {}
    real = ce.create_sdk_mcp_server

    def _capture(name, version="1.0.0", tools=None):
        captured["tools"] = tools
        return real(name, version=version, tools=tools)

    ce.create_sdk_mcp_server = _capture
    try:
        executor._build_ask_mcp_server()
    finally:
        ce.create_sdk_mcp_server = real
    h = captured["tools"][0].handler

    _, _, token = _set_ctx()
    try:
        await h({"questions": QUESTIONS})
    finally:
        _ask_context_var.reset(token)

    assert seen["gateway"] is executor._gateway


@pytest.mark.asyncio
async def test_handler_already_pending_returns_tool_error(handler, monkeypatch):
    async def fake_ask(*a, **k):
        raise AskAlreadyPending("busy")

    monkeypatch.setattr(ce, "ask_user_question", fake_ask)

    _, calls, token = _set_ctx()
    try:
        result = await handler({"questions": QUESTIONS})
    finally:
        _ask_context_var.reset(token)

    assert result["is_error"] is True
    assert "already pending" in result["content"][0]["text"].lower()
    # still resumes the watchdog even on the error path
    assert calls["pause"] == 1
    assert calls["resume"] == 1


@pytest.mark.asyncio
async def test_handler_timed_out_returns_no_response_result(handler, monkeypatch):
    async def fake_ask(chat_id, thread_id, questions, *, gateway, timeout, **kw):
        return {
            "answers": [
                {"header": "Deploy?", "question": "Should I deploy to prod?",
                 "selected": ["__NO_RESPONSE__"]}
            ],
            "timed_out": True,
        }

    monkeypatch.setattr(ce, "ask_user_question", fake_ask)

    _, _, token = _set_ctx()
    try:
        result = await handler({"questions": QUESTIONS})
    finally:
        _ask_context_var.reset(token)

    assert result.get("is_error") is not True
    payload = json.loads(result["content"][0]["text"])
    assert payload["timed_out"] is True


@pytest.mark.asyncio
async def test_handler_without_context_fails_closed(handler, monkeypatch):
    """No active run context -> tool error rather than sending to nowhere."""
    async def fake_ask(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("ask_user_question should not be called")

    monkeypatch.setattr(ce, "ask_user_question", fake_ask)

    # ensure no context is set
    token = _ask_context_var.set(None)
    try:
        result = await handler({"questions": QUESTIONS})
    finally:
        _ask_context_var.reset(token)

    assert result["is_error"] is True


# ── Registration / steering ───────────────────────────────────────────────


def test_ask_server_registered_in_mcp(executor):
    assert _ASK_MCP_SERVER in executor._mcp


def test_build_options_includes_ask_server_and_disallows_native(executor):
    opts = executor._build_options()
    assert _ASK_MCP_SERVER in opts.mcp_servers
    assert "AskUserQuestion" in opts.disallowed_tools


def test_build_options_steers_to_mcp_tool_in_system_prompt(executor):
    opts = executor._build_options()
    assert _ASK_MCP_TOOL_FQN in (opts.append_system_prompt or "")


# ── Stall watchdog pause during a pending ask ──────────────────────────────


@pytest.mark.asyncio
async def test_stall_watchdog_paused_while_question_pending(
    executor, mock_config, monkeypatch
):
    """A run that parks an ask and stays SDK-silent past the stall timeout
    must NOT be cancelled — _execute pauses the stall watchdog while the
    pause hook is engaged."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from superpos_agent_core import ExecutionRequest

    # Tiny stall timeout so the watchdog would fire quickly if not paused.
    mock_config.claude_stall_timeout = 1
    mock_config.executor_max_turns = 5

    req = ExecutionRequest(prompt="hi", chat_id="c1", source="telegram")

    # The inner coro emulates _execute_inner: it engages the watchdog pause
    # (as the ask tool handler does), stays silent for >2x stall_timeout, then
    # resumes and finishes — exactly the human-takes-a-while scenario.
    async def fake_inner(
        req, streamer, retries, *, pre_resolved=None, progress_event=None,
        pause_watchdog=None, resume_watchdog=None,
    ):
        pause_watchdog()
        await asyncio.sleep(2.5)  # > 2 * stall_timeout
        resume_watchdog()

    with patch.object(executor, "_execute_inner", fake_inner), \
         patch("superpos_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        claim_expired = asyncio.Event()
        await executor._execute(req, claim_expired, retries=1)

    # If the watchdog had falsely fired, it would have cancelled the inner
    # task and the streamer would have received a stall error.
    streamer.error.assert_not_called()

