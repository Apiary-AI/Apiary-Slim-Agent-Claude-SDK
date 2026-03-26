import asyncio

import httpx
import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.claude_executor import ClaudeExecutor, ExecutionRequest
from src.config import Config


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
        # Put on queue so task_done() in _run_one works correctly
        await executor.queue.put(req)
        await executor.queue.get()  # simulate run() pulling from queue
        await asyncio.wait_for(executor._run_one(req), timeout=2.0)

    assert not executor.has_apiary_task("task-x")


# --- _build_options uses cwd override when provided ---

def test_build_options_default_cwd(executor, mock_config):
    mock_config.claude_working_dir = "/workspace"
    opts = executor._build_options()
    assert str(opts.cwd) == "/workspace"


def test_build_options_cwd_override(executor, mock_config):
    mock_config.claude_working_dir = "/workspace"
    opts = executor._build_options(cwd="/workspace/.worktrees/feature-x")
    assert str(opts.cwd) == "/workspace/.worktrees/feature-x"


# --- _build_options persona injection ---

def test_build_options_injects_persona_when_set(executor_with_persona):
    opts = executor_with_persona._build_options()
    assert opts.append_system_prompt == "You are a helpful assistant."


def test_build_options_no_system_prompt_when_persona_none(executor):
    opts = executor._build_options()
    assert opts.append_system_prompt is None


def test_build_options_combines_persona_and_system_prompt_append(executor_with_persona):
    opts = executor_with_persona._build_options(system_prompt_append="## Extra\nDo stuff.")
    assert opts.append_system_prompt == "You are a helpful assistant.\n\n## Extra\nDo stuff."


def test_build_options_system_prompt_append_only(executor):
    opts = executor._build_options(system_prompt_append="## Hint\nDo worktree.")
    assert opts.append_system_prompt == "## Hint\nDo worktree."


# --- _execute_inner calls ensure_worktree when branch + isolation enabled ---

async def test_execute_inner_calls_ensure_worktree_for_apiary_with_branch(
    executor, mock_apiary, mock_config
):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="review PR", chat_id="123", source="apiary",
        apiary_task_id="task-wt", branch="feature/my-branch",
    )

    async def _empty():
        return
        yield  # makes it an async generator

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-my-branch"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/my-branch")


# --- _execute_inner calls ensure_worktree for telegram source with explicit branch ---

async def test_execute_inner_telegram_with_explicit_branch_creates_worktree(
    executor, mock_config
):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="telegram", branch="feature/x",
    )

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-x"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/x")


# --- _execute_inner skips worktree when isolation disabled ---

async def test_execute_inner_skips_worktree_when_isolation_disabled(executor, mock_config):
    mock_config.claude_worktree_isolation = False
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="apiary",
        apiary_task_id="task-no-wt", branch="some-branch",
    )

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_not_called()


# --- _execute_inner falls back gracefully when ensure_worktree fails ---

async def test_execute_inner_falls_back_when_worktree_fails(executor, mock_config):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="apiary",
        apiary_task_id="task-fail", branch="bad-branch",
    )

    captured_cwd = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_cwd.append(cwd)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.side_effect = RuntimeError("git error")
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    # cwd should be None (fall back to config default) when worktree creation failed
    assert captured_cwd[0] is None


# --- _execute_inner injects worktree hint for Telegram without branch ---

async def test_execute_inner_injects_worktree_hint_for_telegram_with_isolation(
    executor, mock_config
):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "Worktree Isolation" in captured_appends[0]
    assert "/workspace/.worktrees/<branch>" in captured_appends[0]


async def test_execute_inner_git_branching_hint_when_isolation_disabled(executor, mock_config):
    """When worktree isolation is off, agent still gets git branching instructions."""
    mock_config.claude_worktree_isolation = False
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "origin/main" in captured_appends[0]
    assert "NEVER branch from the current HEAD" in captured_appends[0]


async def test_execute_inner_exits_on_auth_error(executor, mock_config):
    mock_config.claude_worktree_isolation = False

    req = ExecutionRequest(prompt="hello", chat_id="123", source="telegram")

    auth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error","message":"Invalid authentication credentials"}}'
    )

    with patch("src.claude_executor.query", side_effect=auth_error), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer, \
         patch("sys.exit") as mock_exit:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    mock_exit.assert_called_once_with(1)


async def test_execute_inner_exits_on_oauth_expired(executor, mock_config):
    mock_config.claude_worktree_isolation = False

    req = ExecutionRequest(prompt="hello", chat_id="123", source="telegram")

    oauth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired. '
        'Please obtain a new token or refresh your existing token."}}'
    )

    with patch("src.claude_executor.query", side_effect=oauth_error), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer, \
         patch("sys.exit") as mock_exit:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    mock_exit.assert_called_once_with(1)


# --- has_free_slots ---

def test_has_free_slots_true_when_idle(executor):
    assert executor.has_free_slots


def test_has_free_slots_false_at_capacity(executor, mock_config):
    for i in range(mock_config.claude_max_parallel):
        executor.add_apiary_task(f"task-{i}")
    assert not executor.has_free_slots


# --- _resolve_slot ---

def test_resolve_slot_main_for_no_branch(executor, mock_config):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"
    req = ExecutionRequest(prompt="hi", chat_id="1", source="telegram")
    assert executor._resolve_slot(req) == "__main__"


def test_resolve_slot_worktree_path_for_branch(executor, mock_config):
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"
    req = ExecutionRequest(prompt="hi", chat_id="1", source="apiary", branch="feat/x")
    with patch("src.claude_executor.is_git_repo", return_value=True):
        result = executor._resolve_slot(req)
    assert result == "/workspace/.worktrees/feat-x"


# --- Status transitions ---

async def test_status_busy_on_first_task_only(executor, mock_apiary, mock_config):
    """update_status('busy') is called once when two tasks run in parallel on different branches."""
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="apiary", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="apiary", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.3)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    busy_calls = [c for c in mock_apiary.update_status.call_args_list if c.args == ("busy",)]
    assert len(busy_calls) == 1


async def test_status_online_when_all_done(executor, mock_apiary, mock_config):
    """update_status('online') is called only when the last task finishes."""
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="apiary", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="apiary", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.4)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    online_calls = [c for c in mock_apiary.update_status.call_args_list if c.args == ("online",)]
    # Both tasks run in parallel on different branches, so online is called once when both finish
    assert len(online_calls) == 1


# --- Same-branch serialization ---

async def test_same_branch_tasks_serialize(executor, mock_config):
    """Two tasks targeting the same branch must not overlap."""
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    execution_log = []

    async def fake_execute_inner(req, streamer, retries):
        execution_log.append(f"start-{req.prompt}")
        await asyncio.sleep(0.05)
        execution_log.append(f"end-{req.prompt}")

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("src.claude_executor.is_git_repo", return_value=True), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()

        req1 = ExecutionRequest(prompt="first", chat_id="1", source="apiary", branch="same-branch")
        req2 = ExecutionRequest(prompt="second", chat_id="1", source="apiary", branch="same-branch")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.3)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    # Because they share the same worktree lock, first must finish before second starts
    assert execution_log.index("end-first") < execution_log.index("start-second")


async def test_execute_inner_injects_worktree_hint_for_apiary_without_branch(
    executor, mock_apiary, mock_config
):
    """Apiary tasks without an explicit branch should get worktree instructions
    so Claude branches from origin/main instead of the current HEAD."""
    mock_config.claude_worktree_isolation = True
    mock_config.claude_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do apiary task", chat_id="123", source="apiary",
        apiary_task_id="task-999",
    )

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("src.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("src.claude_executor.query", return_value=_empty()), \
         patch("src.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "Worktree Isolation" in captured_appends[0]
    assert "origin/main" in captured_appends[0]
