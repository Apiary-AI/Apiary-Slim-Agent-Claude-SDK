import asyncio

import pytest
from unittest.mock import AsyncMock, Mock, patch

from slim_agent_claude.claude_executor import ClaudeExecutor, ExecutionRequest
from slim_agent_claude.config import ClaudeConfig as Config


# --- Dedup method unit tests (pure sync logic) ---

def test_has_task_initially_false(executor):
    assert not executor.has_superpos_task("abc")


def test_add_then_has(executor):
    executor.add_superpos_task("abc")
    assert executor.has_superpos_task("abc")


def test_remove_clears_task(executor):
    executor.add_superpos_task("abc")
    executor.remove_superpos_task("abc")
    assert not executor.has_superpos_task("abc")


def test_remove_nonexistent_is_safe(executor):
    executor.remove_superpos_task("nonexistent")  # must not raise


# --- report_progress: core function is wired correctly ---

async def test_run_one_uses_core_report_progress(executor, mock_superpos):
    """_run_one must delegate to superpos_agent_core.report_progress
    (not a local method) so that silence detection and robust logging
    are handled by the core implementation."""
    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="superpos", superpos_task_id="task-rp"
    )

    async def fake_execute(req, claim_expired, pre_resolved=None):
        await asyncio.sleep(0)

    with patch.object(executor, "_execute", side_effect=fake_execute), \
         patch("slim_agent_claude.claude_executor.report_progress", new_callable=AsyncMock) as mock_rp, \
         patch("slim_agent_claude.claude_executor.TelegramStreamer"):
        await executor.queue.put(req)
        await executor.queue.get()
        await executor._run_one(req)

    mock_rp.assert_called_once_with(mock_superpos, "task-rp", mock_rp.call_args.args[2])


# --- Claim expiry removes task from in-flight set ---

async def test_execute_removes_task_after_claim_expiry(executor):
    executor.add_superpos_task("task-x")

    async def fake_report_progress(client, task_id, claim_expired, **kwargs):
        claim_expired.set()

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None):
        await asyncio.sleep(10)  # blocks until cancelled

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="superpos", superpos_task_id="task-x"
    )

    with patch("slim_agent_claude.claude_executor.report_progress", fake_report_progress), \
         patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        # Put on queue so task_done() in _run_one works correctly
        await executor.queue.put(req)
        await executor.queue.get()  # simulate run() pulling from queue
        await asyncio.wait_for(executor._run_one(req), timeout=2.0)

    assert not executor.has_superpos_task("task-x")


# --- _build_options uses cwd override when provided ---

def test_build_options_default_cwd(executor, mock_config):
    mock_config.executor_working_dir = "/workspace"
    opts = executor._build_options()
    assert str(opts.cwd) == "/workspace"


def test_build_options_cwd_override(executor, mock_config):
    mock_config.executor_working_dir = "/workspace"
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

async def test_execute_inner_calls_ensure_worktree_for_superpos_with_branch(
    executor, mock_superpos, mock_config
):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="review PR", chat_id="123", source="superpos",
        superpos_task_id="task-wt", branch="feature/my-branch",
    )

    async def _empty():
        return
        yield  # makes it an async generator

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-my-branch"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/my-branch")


# --- _execute_inner calls ensure_worktree for telegram source with explicit branch ---

async def test_execute_inner_telegram_with_explicit_branch_creates_worktree(
    executor, mock_config
):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="hello", chat_id="123", source="telegram", branch="feature/x",
    )

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feature-x"
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feature/x")


# --- _execute_inner skips worktree when isolation disabled ---

async def test_execute_inner_skips_worktree_when_isolation_disabled(executor, mock_config):
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="superpos",
        superpos_task_id="task-no-wt", branch="some-branch",
    )

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_not_called()


# --- _execute_inner falls back gracefully when ensure_worktree fails ---

async def test_execute_inner_falls_back_when_worktree_fails(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do it", chat_id="123", source="superpos",
        superpos_task_id="task-fail", branch="bad-branch",
    )

    captured_cwd = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_cwd.append(cwd)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
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
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "Worktree Isolation" in captured_appends[0]
    assert "/workspace/.worktrees/<branch>" in captured_appends[0]


async def test_execute_inner_git_branching_hint_when_isolation_disabled(executor, mock_config):
    """When worktree isolation is off, agent still gets git branching instructions."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(prompt="implement a feature", chat_id="123", source="telegram")

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "origin/main" in captured_appends[0]
    assert "NEVER branch from the current HEAD" in captured_appends[0]


async def test_execute_inner_exits_on_auth_error(executor, mock_config):
    mock_config.executor_worktree_isolation = False

    req = ExecutionRequest(prompt="hello", chat_id="123", source="telegram")

    auth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error","message":"Invalid authentication credentials"}}'
    )

    with patch("slim_agent_claude.claude_executor.query", side_effect=auth_error), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer, \
         patch("sys.exit") as mock_exit:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    mock_exit.assert_called_once_with(1)


async def test_execute_inner_exits_on_oauth_expired(executor, mock_config):
    mock_config.executor_worktree_isolation = False

    req = ExecutionRequest(prompt="hello", chat_id="123", source="telegram")

    oauth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error","message":"OAuth token has expired. '
        'Please obtain a new token or refresh your existing token."}}'
    )

    with patch("slim_agent_claude.claude_executor.query", side_effect=oauth_error), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer, \
         patch("sys.exit") as mock_exit:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    mock_exit.assert_called_once_with(1)


# --- has_free_slots ---

def test_has_free_slots_true_when_idle(executor):
    assert executor.has_free_slots


def test_has_free_slots_false_at_capacity(executor, mock_config):
    for i in range(mock_config.executor_max_parallel):
        executor.add_superpos_task(f"task-{i}")
    assert not executor.has_free_slots


# --- _resolve_slot ---
# Signature takes the *effective* branch (post-SessionStore restoration),
# not the raw request, so the lock key matches the cwd _execute_inner uses.

def test_resolve_slot_main_for_no_branch(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    assert executor._resolve_slot(None) == "__main__"


def test_resolve_slot_worktree_path_for_branch(executor, mock_config):
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True):
        result = executor._resolve_slot("feat/x")
    assert result == "/workspace/.worktrees/feat-x"


# --- Status transitions ---

async def test_status_busy_on_first_task_only(executor, mock_superpos, mock_config):
    """update_status('busy') is called once when two tasks run in parallel on different branches."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="superpos", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="superpos", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.3)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    busy_calls = [c for c in mock_superpos.update_status.call_args_list if c.args == ("busy",)]
    assert len(busy_calls) == 1


async def test_status_online_when_all_done(executor, mock_superpos, mock_config):
    """update_status('online') is called only when the last task finishes."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None):
        await asyncio.sleep(0.1)

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        req1 = ExecutionRequest(prompt="a", chat_id="1", source="superpos", branch="branch-a")
        req2 = ExecutionRequest(prompt="b", chat_id="1", source="superpos", branch="branch-b")
        await executor.queue.put(req1)
        await executor.queue.put(req2)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.4)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    online_calls = [c for c in mock_superpos.update_status.call_args_list if c.args == ("online",)]
    # Both tasks run in parallel on different branches, so online is called once when both finish
    assert len(online_calls) == 1


# --- Same-branch serialization ---

async def test_same_branch_tasks_serialize(executor, mock_config):
    """Two tasks targeting the same branch must not overlap."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    execution_log = []

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None):
        execution_log.append(f"start-{req.prompt}")
        await asyncio.sleep(0.05)
        execution_log.append(f"end-{req.prompt}")

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()

        req1 = ExecutionRequest(prompt="first", chat_id="1", source="superpos", branch="same-branch")
        req2 = ExecutionRequest(prompt="second", chat_id="1", source="superpos", branch="same-branch")
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


async def test_execute_inner_injects_worktree_hint_for_superpos_without_branch(
    executor, mock_superpos, mock_config
):
    """Superpos tasks without an explicit branch should get worktree instructions
    so Claude branches from origin/main instead of the current HEAD."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do superpos task", chat_id="123", source="superpos",
        superpos_task_id="task-999",
    )

    captured_appends = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(resume_session=resume_session, cwd=cwd, system_prompt_append=system_prompt_append)

    async def _empty():
        return
        yield

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", return_value=_empty()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.finish = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends[0] is not None
    assert "Worktree Isolation" in captured_appends[0]
    assert "origin/main" in captured_appends[0]


# --- ProcessError retry with session resume ---

async def test_execute_inner_resumes_session_after_cli_crash(
    executor, mock_superpos, mock_config
):
    """When the Claude CLI subprocess crashes mid-task, the executor must
    retry with --resume <session_id> so prior side effects (commits, PR
    comments) aren't duplicated.

    The SDK's _read_messages task swallows the underlying ProcessError and
    re-raises it as a plain Exception via an in-band error queue message
    (query.py:491), so the agent's catch block sees `Exception`, not
    `ProcessError`. This test mirrors that real production path.
    """
    from claude_code_sdk.types import ResultMessage

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-crash",
    )

    captured_resumes = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    result_msg = ResultMessage(
        subtype="success", duration_ms=100, duration_api_ms=80,
        is_error=False, num_turns=1, session_id="sess-abc",
    )

    async def crash_then_succeed():
        # First call: yield session_id then crash with the same message
        # the SDK actually surfaces (plain Exception, not ProcessError).
        yield result_msg
        raise Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )

    async def succeed():
        return
        yield  # noqa — make it an async generator

    call_count = {"n": 0}

    def query_side_effect(*args, **kwargs):
        call_count["n"] += 1
        return crash_then_succeed() if call_count["n"] == 1 else succeed()

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=query_side_effect), \
         patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    # First attempt without resume, second attempt resumes the captured session
    assert captured_resumes == [None, "sess-abc"]
    # Task should have been completed (after successful retry), not failed
    assert mock_superpos.complete_task.await_count == 1
    assert mock_superpos.fail_task.await_count == 0


async def test_execute_inner_no_resume_when_crash_before_first_session_id(
    executor, mock_superpos, mock_config
):
    """A CLI crash before any ResultMessage carries no session_id, so we
    cannot resume — the executor must mark the task failed instead of
    looping."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-early-crash",
    )

    async def crash_immediately():
        raise Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )
        yield  # noqa — make it an async generator

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: crash_immediately()), \
         patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    assert mock_superpos.fail_task.await_count == 1
    assert mock_superpos.complete_task.await_count == 0


def test_read_captured_stderr_returns_tail():
    """The stderr-capture helper must tail large files to max_bytes."""
    import tempfile
    from slim_agent_claude.claude_executor import _read_captured_stderr

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        # 5KB of payload — bigger than the default 4KB tail
        f.write("HEAD\n" + ("x" * 5000) + "\nTAIL_MARKER")
        path = f.name
    try:
        out = _read_captured_stderr(path, max_bytes=4096)
        assert "TAIL_MARKER" in out
        assert "(truncated)" in out
        assert "HEAD" not in out  # truncated away
    finally:
        import os
        os.unlink(path)


def test_read_captured_stderr_handles_missing_or_empty():
    from slim_agent_claude.claude_executor import _read_captured_stderr
    assert _read_captured_stderr(None) == ""
    assert _read_captured_stderr("/nonexistent/path/xyz.log") == ""


async def test_execute_inner_includes_captured_stderr_in_fail_summary(
    executor, mock_superpos, mock_config
):
    """When the open_process patch captured CLI stderr, the fail_task
    summary must include the tail so the operator can diagnose root cause
    without trawling docker logs."""
    import tempfile
    from slim_agent_claude.claude_executor import _stderr_capture_var

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    # Pre-populate a tempfile as if the CLI had crashed with this stderr
    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write("FATAL: out of memory\nclaude: aborted\n")
        stderr_path = f.name

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-with-stderr",
    )

    async def crash_after_populating_capture():
        # Simulate what the real anyio.open_process patch does: stash the
        # captured stderr path on the contextvar's capture dict, then let
        # the SDK raise the wrapped Exception.
        capture = _stderr_capture_var.get()
        if capture is not None:
            capture["path"] = stderr_path
            capture["pid"] = 12345
        raise Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )
        yield  # noqa

    try:
        with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
             patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: crash_after_populating_capture()), \
             patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
             patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
            streamer = MockStreamer.return_value
            streamer.start = AsyncMock()
            streamer.finish = AsyncMock()
            streamer.append = AsyncMock()
            streamer.error = AsyncMock()
            streamer.send_tool_notification = AsyncMock()
            await executor._execute_inner(req, streamer, retries=1)
    finally:
        import os
        if os.path.exists(stderr_path):
            os.unlink(stderr_path)

    # fail_task summary should carry the captured stderr tail
    assert mock_superpos.fail_task.await_count == 1
    summary = mock_superpos.fail_task.await_args.kwargs["summary"]
    assert "cli_stderr" in summary
    assert "out of memory" in summary["cli_stderr"]

    # Telegram error message should include the actual stderr, not the
    # generic "check docker logs" placeholder.
    sent = streamer.error.await_args.args[0]
    assert "out of memory" in sent


async def test_execute_inner_process_error_failure_includes_docker_logs_hint(
    executor, mock_superpos, mock_config
):
    """When a CLI crash is unrecoverable AND no stderr was captured, the
    streamer error message must point the operator at docker logs so they
    can diagnose externally."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-crash-final",
    )

    async def crash_immediately():
        raise Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: crash_immediately()), \
         patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert streamer.error.await_count == 1
    sent = streamer.error.await_args.args[0]
    assert "docker logs" in sent


async def test_execute_inner_resumes_session_from_init_message(
    executor, mock_superpos, mock_config
):
    """Production crash path: the CLI emits a SystemMessage(subtype="init")
    carrying session_id at the start of every run, then crashes mid-stream
    before any ResultMessage. The executor must capture session_id from init
    — relying on the terminal ResultMessage alone leaves crash-retry without
    a session to resume in the exact failure mode this code is meant to fix.
    """
    from claude_code_sdk.types import SystemMessage

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-init-crash",
    )

    captured_resumes = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    init_msg = SystemMessage(
        subtype="init",
        data={"session_id": "sess-from-init", "type": "system", "subtype": "init"},
    )

    async def crash_after_init():
        yield init_msg
        raise Exception(
            "Command failed with exit code 1 (exit code: 1)\n"
            "Error output: Check stderr output for details"
        )

    async def succeed():
        return
        yield  # noqa — make it an async generator

    call_count = {"n": 0}

    def query_side_effect(*args, **kwargs):
        call_count["n"] += 1
        return crash_after_init() if call_count["n"] == 1 else succeed()

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=query_side_effect), \
         patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=3)

    assert captured_resumes == [None, "sess-from-init"]
    assert mock_superpos.complete_task.await_count == 1
    assert mock_superpos.fail_task.await_count == 0


async def test_execute_inner_cleans_up_stderr_tempfile_on_cancellation(
    executor, mock_superpos, mock_config
):
    """Claim-expiry and task-timeout cancel _execute_inner via CancelledError,
    which is a BaseException — `except (ClaudeSDKError, Exception)` doesn't
    catch it. Without try/finally cleanup the delete=False stderr tempfile
    leaks on every cancellation; in a long-running worker handling many tasks
    this exhausts /tmp.
    """
    import os
    import tempfile
    from slim_agent_claude.claude_executor import _stderr_capture_var

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="do work", chat_id="123", source="superpos",
        superpos_task_id="task-cancelled",
    )

    created_path = {"value": None}

    async def populate_capture_then_cancel():
        # Simulate the open_process patch: stash a real tempfile path on the
        # capture dict, then raise CancelledError mid-stream.
        capture = _stderr_capture_var.get()
        f = tempfile.NamedTemporaryFile(
            mode="w", prefix="claude-stderr-test-", suffix=".log", delete=False,
        )
        f.write("simulated cli stderr\n")
        f.close()
        if capture is not None:
            capture["path"] = f.name
        created_path["value"] = f.name
        raise asyncio.CancelledError()
        yield  # noqa — keeps this an async generator

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: populate_capture_then_cancel()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        with pytest.raises(asyncio.CancelledError):
            await executor._execute_inner(req, streamer, retries=1)

    assert created_path["value"] is not None
    assert not os.path.exists(created_path["value"]), (
        f"stderr tempfile leaked on CancelledError: {created_path['value']}"
    )


# --- Recent-tasks bridge from Superpos → Telegram ---

async def test_telegram_request_sees_recent_superpos_task_in_system_prompt(
    executor, mock_superpos, mock_config
):
    """A Telegram message that arrives after a Superpos task completed in the
    same chat must see the task's summary appended to the system prompt, so
    the user can ask follow-up questions about notifications they saw."""
    from superpos_agent_core import TaskSummary

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    # Pre-populate the recent-tasks log as if a Superpos task had just run
    executor._recent_tasks.record(
        "chat-77",
        TaskSummary(
            task_id="sp-task-99",
            description="Deploy frontend to staging",
            outcome="failed",
            detail="kubectl timeout after 60s",
        ),
    )

    req = ExecutionRequest(
        prompt="what went wrong with the deploy?",
        chat_id="chat-77",
        source="telegram",
    )

    captured_appends: list[str | None] = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_appends.append(system_prompt_append)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_appends, "_build_options should have been called"
    appended = captured_appends[0]
    assert appended is not None
    assert "sp-task-99" in appended
    assert "Deploy frontend to staging" in appended
    assert "kubectl timeout" in appended
    assert "failed" in appended


async def test_superpos_task_completion_records_summary(
    executor, mock_superpos, mock_config
):
    """A successfully completed Superpos task must land in the recent-tasks
    log so the next Telegram message in the same chat picks it up."""
    from claude_code_sdk.types import AssistantMessage, TextBlock, ResultMessage

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="build the report", chat_id="chat-77", source="superpos",
        superpos_task_id="sp-task-record",
    )

    async def stream_then_finish():
        yield AssistantMessage(content=[TextBlock(text="Report generated.")], model="m")
        yield ResultMessage(
            subtype="success", duration_ms=100, duration_api_ms=80,
            is_error=False, num_turns=1, session_id="sess-x",
        )

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: stream_then_finish()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    rendered = executor._recent_tasks.render("chat-77")
    assert rendered is not None
    assert "sp-task-record" in rendered
    assert "succeeded" in rendered
    assert "build the report" in rendered


async def test_superpos_task_failure_records_summary(
    executor, mock_superpos, mock_config
):
    """Failures must also land in the recent-tasks log — the user often
    asks follow-up questions about what went wrong."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(
        prompt="risky operation", chat_id="chat-77", source="superpos",
        superpos_task_id="sp-task-fail",
    )

    async def fail_immediately():
        raise Exception("simulated failure")
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: fail_immediately()), \
         patch("slim_agent_claude.claude_executor.asyncio.sleep", new_callable=AsyncMock), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    rendered = executor._recent_tasks.render("chat-77")
    assert rendered is not None
    assert "sp-task-fail" in rendered
    assert "failed" in rendered
    assert "simulated failure" in rendered


# --- Preflight cleanup ---

async def test_preflight_flips_offline_before_exit_on_auth_failure(
    executor, mock_superpos,
):
    """Core's run_agent calls update_status('online') before executor.preflight(),
    so a sys.exit(1) on bad credentials would otherwise leave the agent showing
    as online in Superpos until heartbeat timeout. Verify preflight flips it
    offline on its way out.
    """
    auth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error",'
        '"message":"Invalid authentication credentials"}}'
    )

    async def _raise_auth_error():
        raise auth_error
        yield  # noqa — async generator marker

    with patch(
        "slim_agent_claude.claude_executor.query",
        side_effect=lambda *a, **kw: _raise_auth_error(),
    ), patch("sys.exit") as mock_exit:
        await executor.preflight()

    mock_exit.assert_called_once_with(1)
    # Critical: offline status must be sent BEFORE the exit
    mock_superpos.update_status.assert_awaited_with("offline")


async def test_preflight_flips_offline_on_unknown_failure(
    executor, mock_superpos,
):
    """The auth-message-pattern matcher only recognises a few known errors;
    everything else re-raises so run_agent itself exits. The offline flip
    must happen on this path too.
    """
    unknown_error = RuntimeError("something went sideways")

    async def _raise_unknown():
        raise unknown_error
        yield  # noqa

    with patch(
        "slim_agent_claude.claude_executor.query",
        side_effect=lambda *a, **kw: _raise_unknown(),
    ):
        with pytest.raises(RuntimeError):
            await executor.preflight()

    mock_superpos.update_status.assert_awaited_with("offline")


async def test_preflight_no_offline_call_when_no_superpos(
    mock_config, mock_runtime, mock_gateway,
):
    """When Superpos integration is disabled the executor has no client to
    notify — preflight must still bail cleanly without raising AttributeError.
    """
    executor = ClaudeExecutor(mock_config, mock_runtime, None, mock_gateway)

    auth_error = Exception(
        '{"type":"error","error":{"type":"authentication_error",'
        '"message":"Invalid authentication credentials"}}'
    )

    async def _raise_auth_error():
        raise auth_error
        yield  # noqa

    with patch(
        "slim_agent_claude.claude_executor.query",
        side_effect=lambda *a, **kw: _raise_auth_error(),
    ), patch("sys.exit") as mock_exit:
        await executor.preflight()

    mock_exit.assert_called_once_with(1)


# --- Cleanup paths ---

def test_cleanup_stale_sessions_uses_hyphenated_session_env_path(
    executor, tmp_path, monkeypatch,
):
    """Claude writes per-session env snapshots to `~/.claude/session-env`
    (hyphenated). The earlier `session_env` (underscore) path was a typo
    that silently never matched, so snapshots accumulated indefinitely.
    Lock in the right name so we can't regress.
    """
    import os
    import time

    fake_home = tmp_path
    monkeypatch.setenv("HOME", str(fake_home))

    # Stale snapshot under the CORRECT (hyphenated) path
    real_dir = fake_home / ".claude" / "session-env" / "sess-old"
    real_dir.mkdir(parents=True)
    (real_dir / "snapshot.json").write_text('{"k": "v"}')
    old = time.time() - (48 * 3600)
    os.utime(real_dir, (old, old))

    # Decoy snapshot under the BROKEN (underscore) path — must be untouched
    decoy_dir = fake_home / ".claude" / "session_env" / "sess-decoy"
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "snapshot.json").write_text('{"k": "v"}')
    os.utime(decoy_dir, (old, old))

    counts = executor.cleanup_stale_sessions(max_age_hours=24)

    assert counts["session_env"] == 1, (
        f"expected exactly one snapshot cleaned, got {counts}"
    )
    assert not real_dir.exists(), "real session-env snapshot should be deleted"
    assert decoy_dir.exists(), (
        "underscore-path decoy must NOT be touched (would be a typo regression)"
    )


# --- Persona version tracking & lazy session invalidation ---

def test_update_persona_tracks_version(executor):
    executor.update_persona("first persona", version=1)
    assert executor._persona == "first persona"
    assert executor._persona_version == 1


def test_update_persona_without_version_keeps_prior_version(executor):
    """A subsequent update_persona(prompt, version=None) refreshes the
    prompt but does NOT erase the previously-known version — the poller
    sometimes pushes a refreshed prompt with no version (e.g. when only
    platform_context changed) and we need the persona-version comparison
    on the next resume to keep working."""
    executor.update_persona("first", version=2)
    executor.update_persona("second", version=None)
    assert executor._persona == "second"
    assert executor._persona_version == 2


def test_update_persona_bump_logs_invalidation_intent(executor, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="slim_agent_claude.claude_executor")
    executor.update_persona("v1", version=1)
    executor.update_persona("v2", version=2)
    assert any(
        "Persona version bumped 1 -> 2" in r.message for r in caplog.records
    )


def test_update_persona_first_set_does_not_log_bump(executor, caplog):
    """First-time set of version (None → N) is just initialisation,
    not a bump — no chat sessions exist yet under any earlier persona,
    so logging "invalidated on next use" would be noise."""
    import logging
    caplog.set_level(logging.INFO, logger="slim_agent_claude.claude_executor")
    executor.update_persona("v1", version=1)
    assert not any("Persona version bumped" in r.message for r in caplog.records)


def test_update_persona_does_not_spawn_background_sub_agent_sync(executor):
    """SubAgentDefinition re-sync after a persona bump is owned by
    ``superpos_agent_core.superpos_poller._resync_sub_agents``.  The
    executor must NOT start its own duplicate background thread on the
    same persona bump — that would race the core sync on
    ``.claude/subagents`` (duplicate HTTP traffic + concurrent writes)."""
    import threading as _threading

    # Belt-and-braces: the helper method should be gone entirely.
    assert not hasattr(executor, "_sync_sub_agents_background"), (
        "Executor must not own a sub-agent sync path — core's poller owns it"
    )

    before = _threading.active_count()
    executor.update_persona("v1", version=1)
    executor.update_persona("v2", version=2)  # triggers the "bump" branch
    # No new daemon threads should have been spawned by update_persona.
    assert _threading.active_count() == before


async def test_resume_dropped_when_stored_version_older_than_current(
    executor, mock_config,
):
    """The core invariant: a session started under persona v1 must NOT
    be resumed once the executor is on v2.  Otherwise the LLM inherits
    its old identity from the resumed transcript."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 5
    executor._sessions.set_with_version("chat-old", "sess-old", 1)

    req = ExecutionRequest(prompt="hello", chat_id="chat-old", source="telegram")

    captured_resumes: list[str | None] = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_resumes == [None], (
        "stale-persona session must not be resumed"
    )
    assert executor._sessions.get_with_version("chat-old") is None, (
        "stale session must be cleared from the store"
    )


async def test_resume_kept_when_versions_match(executor, mock_config):
    """A session started under the current persona must still resume."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 3
    executor._sessions.set_with_version("chat-current", "sess-current", 3)

    req = ExecutionRequest(
        prompt="follow-up", chat_id="chat-current", source="telegram",
    )

    captured_resumes: list[str | None] = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_resumes == ["sess-current"]


async def test_unversioned_session_invalidated_once_persona_version_known(
    executor, mock_config,
):
    """Startup race: Telegram polling can write a session before the
    Superpos persona-version poller has populated `_persona_version`,
    so the session is saved with persona_version=None.  Once a version
    becomes known, that None must be treated as "older than current"
    and invalidated — otherwise those chats are permanently exempt from
    persona refreshes.
    """
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    # The race: session stored while _persona_version was None
    executor._persona_version = None
    executor._sessions.set_with_version("chat-race", "sess-stale", None)

    # Superpos poller has since run
    executor._persona_version = 5

    req = ExecutionRequest(prompt="hello", chat_id="chat-race", source="telegram")

    captured_resumes: list[str | None] = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_resumes == [None]
    assert executor._sessions.get_with_version("chat-race") is None


async def test_unversioned_session_kept_when_persona_version_not_known(
    executor, mock_config,
):
    """Inverse of the race fix: when `_persona_version` is still None,
    an unversioned stored session has no basis for comparison and must
    be resumed.  This is the early-startup state before the first
    persona-version poll completes."""
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = None
    executor._sessions.set_with_version("chat-q", "sess-q", None)

    req = ExecutionRequest(prompt="hello", chat_id="chat-q", source="telegram")

    captured_resumes: list[str | None] = []
    original_build = executor._build_options

    def capture_build(resume_session=None, cwd=None, system_prompt_append=None):
        captured_resumes.append(resume_session)
        return original_build(
            resume_session=resume_session, cwd=cwd,
            system_prompt_append=system_prompt_append,
        )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch.object(executor, "_build_options", side_effect=capture_build), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert captured_resumes == ["sess-q"]


async def test_new_session_id_stored_with_current_persona_version(
    executor, mock_config,
):
    """After a successful run, the session_id should be saved with the
    *current* `_persona_version` so the next message can compare against
    a future persona bump."""
    from claude_code_sdk.types import SystemMessage

    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 7

    req = ExecutionRequest(prompt="hello", chat_id="chat-new", source="telegram")

    init_msg = SystemMessage(
        subtype="init",
        data={"session_id": "sess-fresh", "type": "system", "subtype": "init"},
    )

    async def stream_init_then_finish():
        yield init_msg

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=False), \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: stream_init_then_finish()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert executor._sessions.get_with_version("chat-new") == ("sess-fresh", 7, None)


# --- /stop wiring ---------------------------------------------------------


async def test_execute_tracks_chat_task_for_stop(executor, mock_config):
    """Core's base-class ``cancel_chat`` can only see in-flight work if
    executors register their inner task via ``_track_chat_task``.  Verify
    that hook actually runs from ``_execute`` so /stop is real.
    """
    mock_config.executor_worktree_isolation = False
    mock_config.executor_working_dir = "/workspace"

    req = ExecutionRequest(prompt="hi", chat_id="chat-stop", source="telegram")

    started = asyncio.Event()
    inner_was_cancelled = asyncio.Event()

    async def slow_execute_inner(_req, _streamer, _retries, **_kwargs):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            inner_was_cancelled.set()
            raise

    with patch.object(executor, "_execute_inner", slow_execute_inner), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        MockStreamer.return_value.finish = AsyncMock()

        runner = asyncio.create_task(executor._execute(req, asyncio.Event()))
        await asyncio.wait_for(started.wait(), timeout=2.0)

        # _execute should have registered the inner task under "chat-stop"
        assert "chat-stop" in executor._chat_tasks, (
            "executor._track_chat_task must be called from _execute so /stop "
            "can find this in-flight work"
        )

        # Simulate /stop firing
        cancelled = executor.cancel_chat("chat-stop")
        assert cancelled == 1

        await asyncio.wait_for(inner_was_cancelled.wait(), timeout=2.0)

        try:
            await asyncio.wait_for(runner, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

    # Auto-untrack via done callback
    assert "chat-stop" not in executor._chat_tasks


# --- Branch-pinning on session resume ────────────────────────────────────
#
# Bug being fixed: Telegram's branch resolution runs against each message
# independently.  Message 1 ("look at PR #607") sets req.branch via the
# PR-ref regex and the session is started under the worktree cwd.
# Message 2 ("Go") has no PR ref, so req.branch is None, the executor
# falls back to the default cwd, and Claude CLI can't find the transcript
# at ~/.claude/projects/<default-cwd-encoded>/<sid>.jsonl — it silently
# starts a fresh conversation, the agent loses prior context, and the
# user-visible symptom is "you said 'Go' but I have no idea what we
# were doing".  Pinning the branch on the SessionStore entry restores
# the cwd on resume and the transcript loads.

async def test_resume_restores_stored_branch_when_request_has_none(
    executor, mock_config,
):
    """Follow-up Telegram message without an explicit branch must resume
    on the branch stored alongside the session id so Claude CLI looks
    in the right project dir."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 3
    executor._sessions.set_with_version(
        "chat-1", "sess-pr607", 3, branch="feat/issues-phase-1",
    )

    req = ExecutionRequest(
        prompt="Go", chat_id="chat-1", source="telegram",  # no branch
    )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feat-issues-phase-1"
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feat/issues-phase-1")


async def test_resume_keeps_explicit_branch_over_stored_one(
    executor, mock_config,
):
    """When the user explicitly switches branches (--branch or PR ref),
    the request branch wins — the user is deliberately moving context."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 3
    executor._sessions.set_with_version(
        "chat-1", "sess-old-branch", 3, branch="feat/old",
    )

    req = ExecutionRequest(
        prompt="now look at this", chat_id="chat-1", source="telegram",
        branch="feat/new",
    )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feat-new"
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_called_once_with("/workspace", "feat/new")


async def test_session_save_records_effective_branch(
    executor, mock_config,
):
    """The branch saved alongside a new session id must be the *effective*
    branch — the one execution actually ran on — not the raw req.branch.
    Otherwise a resume that fell back to a stored branch would overwrite
    the entry with branch=None and the next message would lose context
    again."""
    from claude_code_sdk.types import SystemMessage

    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 4
    executor._sessions.set_with_version(
        "chat-1", "sess-prev", 4, branch="feat/keep-me",
    )

    req = ExecutionRequest(
        prompt="continue", chat_id="chat-1", source="telegram",  # no branch
    )

    init_msg = SystemMessage(
        subtype="init",
        data={"session_id": "sess-next", "type": "system", "subtype": "init"},
    )

    async def stream_init():
        yield init_msg

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: stream_init()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.return_value = "/workspace/.worktrees/feat-keep-me"
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert executor._sessions.get_with_version("chat-1") == (
        "sess-next", 4, "feat/keep-me",
    ), "effective branch must persist across the resume → re-save cycle"


async def test_resume_falls_back_to_default_when_stored_branch_is_none(
    executor, mock_config,
):
    """Pre-fix entries (and entries from sessions started without a
    branch) have branch=None — those must continue to use the default
    cwd, matching the historical behavior before this field existed."""
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 2
    executor._sessions.set_with_version("chat-1", "sess-no-branch", 2)
    # branch defaults to None — no worktree expected

    req = ExecutionRequest(
        prompt="hi", chat_id="chat-1", source="telegram",
    )

    async def succeed():
        return
        yield  # noqa

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: succeed()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    mock_ensure.assert_not_called()


async def test_resume_target_observes_sessionstore_writes_during_lock_wait(
    executor, mock_config,
):
    """Regression for Codex P1 on resume-target snapshot timing.

    Scenario: msg A is running for chat-1, msg B for the same chat
    queues up.  A finishes and writes a new session id to SessionStore.
    B then acquires the worktree lock and starts executing.  If B
    snapshotted its resume target at queue time, it would resume A's
    *old* session id and lose the most recent context.  The fix
    resolves the resume target *after* the worktree lock is held, so B
    observes A's just-committed write.

    Simulated here by mutating SessionStore inside ``_resolve_slot``,
    which runs after the peek (pre-lock) and before the post-lock
    resolve.  ``pre_resolved`` passed to ``_execute`` must carry the
    new session id.
    """
    mock_config.executor_worktree_isolation = False
    executor._persona_version = 1

    # Stored entry at the time msg B's _run_one starts — i.e., what
    # the old (buggy) pre-lock snapshot would have captured.
    executor._sessions.set_with_version(
        "chat-1", "sess-old", 1, branch=None,
    )

    req = ExecutionRequest(
        prompt="follow-up", chat_id="chat-1", source="telegram",
    )

    captured_pre_resolved: list = []

    async def fake_execute(req, claim_expired, pre_resolved=None):
        captured_pre_resolved.append(pre_resolved)

    original_resolve_slot = executor._resolve_slot

    def resolve_slot_with_concurrent_write(branch):
        # Stand-in for msg A finishing and committing its
        # ``set_with_version`` write while msg B holds the semaphore
        # but hasn't yet resolved its resume target.
        executor._sessions.set_with_version(
            "chat-1", "sess-new", 1, branch=None,
        )
        return original_resolve_slot(branch)

    with patch.object(executor, "_resolve_slot", side_effect=resolve_slot_with_concurrent_write), \
         patch.object(executor, "_execute", side_effect=fake_execute), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer"):
        await executor.queue.put(req)
        await executor.queue.get()
        await executor._run_one(req)

    assert captured_pre_resolved, "_execute was never called"
    resume_id, _branch = captured_pre_resolved[0]
    assert resume_id == "sess-new", (
        f"Expected post-lock resolution to see the new session id "
        f"committed during the lock wait, but got {resume_id!r}"
    )


async def test_lock_swaps_to_canonical_slot_when_branch_diverges_post_resolve(
    executor, mock_config,
):
    """Regression: if a same-chat task writes SessionStore mid-wait,
    the peeked branch can mismatch the post-lock resolved branch.
    Execution must serialize on the resolved slot — otherwise it runs
    in branch B while holding branch A's lock, defeating the
    per-worktree mutex and allowing concurrent git mutations in the
    same tree under ``executor_max_parallel > 1``.
    """
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 1

    # Pre-existing stored entry — what the pre-lock peek sees.
    executor._sessions.set_with_version(
        "chat-1", "sess-old", 1, branch="feat/old",
    )

    req = ExecutionRequest(
        prompt="follow-up", chat_id="chat-1", source="telegram",
    )

    captured_pre_resolved: list = []
    locks_acquired: list[str] = []

    async def fake_execute(req, claim_expired, pre_resolved=None):
        captured_pre_resolved.append(pre_resolved)

    original_resolve_slot = executor._resolve_slot
    original_get_lock = executor._get_worktree_lock

    def get_lock_recording(slot):
        locks_acquired.append(slot)
        return original_get_lock(slot)

    resolve_count = {"n": 0}

    def resolve_slot_with_first_call_swap(branch):
        # On the FIRST call (pre-lock, peeked branch=feat/old), simulate
        # a same-chat task committing a SessionStore write that swaps the
        # branch.  The post-lock resolve will then see the new branch
        # and the loop must re-acquire on its slot.
        resolve_count["n"] += 1
        if resolve_count["n"] == 1:
            executor._sessions.set_with_version(
                "chat-1", "sess-new", 1, branch="feat/new",
            )
        return original_resolve_slot(branch)

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch.object(executor, "_resolve_slot", side_effect=resolve_slot_with_first_call_swap), \
         patch.object(executor, "_get_worktree_lock", side_effect=get_lock_recording), \
         patch.object(executor, "_execute", side_effect=fake_execute), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer"):
        await executor.queue.put(req)
        await executor.queue.get()
        await executor._run_one(req)

    assert captured_pre_resolved, "_execute was never called"
    resume_id, branch = captured_pre_resolved[0]
    assert resume_id == "sess-new" and branch == "feat/new", (
        f"Expected post-lock resolve to land on (sess-new, feat/new); "
        f"got ({resume_id!r}, {branch!r})"
    )
    # Two locks must have been touched: the peeked slot (feat/old) and
    # the post-resolve canonical slot (feat/new).  The peek slot is
    # released and the canonical slot is held for execution.
    assert len(locks_acquired) >= 2, (
        f"Expected at least one lock swap; locks_acquired={locks_acquired!r}"
    )
    assert locks_acquired[0] != locks_acquired[-1], (
        f"Slot swap didn't happen — peek and final slot are identical: "
        f"{locks_acquired!r}"
    )


async def test_session_save_pins_branch_only_when_worktree_used(
    executor, mock_config,
):
    """When ensure_worktree raises, execution falls back to the default
    cwd — the transcript is then written under that default cwd's
    project dir.  The stored branch must be None in that case, otherwise
    a future resume would restore cwd to the worktree path and Claude
    CLI would silently fail to find the transcript (then start fresh).
    """
    from claude_code_sdk.types import SystemMessage

    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 1
    # No prior session; this is a fresh first turn that *requests* a
    # branch, but the worktree creation will fail.
    req = ExecutionRequest(
        prompt="hello", chat_id="chat-x", source="telegram",
        branch="bad-branch",
    )

    init_msg = SystemMessage(
        subtype="init",
        data={"session_id": "sess-new", "type": "system", "subtype": "init"},
    )

    async def stream_init():
        yield init_msg

    with patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.ensure_worktree", new_callable=AsyncMock) as mock_ensure, \
         patch("slim_agent_claude.claude_executor.query", side_effect=lambda *a, **kw: stream_init()), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        mock_ensure.side_effect = RuntimeError("git error")
        streamer = MockStreamer.return_value
        streamer.start = AsyncMock()
        streamer.finish = AsyncMock()
        streamer.append = AsyncMock()
        streamer.error = AsyncMock()
        streamer.send_tool_notification = AsyncMock()
        await executor._execute_inner(req, streamer, retries=1)

    assert executor._sessions.get_with_version("chat-x") == (
        "sess-new", 1, None,
    ), (
        "ensure_worktree failed → transcript lives under default cwd → "
        "stored branch must be None so resumes resolve to the same path"
    )


# --- cleanup_stale_sessions walks every project subdir ─────────────────

def test_cleanup_stale_sessions_walks_worktree_project_dirs(
    executor, tmp_path, monkeypatch,
):
    """Worktree-scoped sessions live under their own
    `projects/<encoded-worktree-cwd>/` dir.  The earlier code only walked
    `projects/-workspace`, so stale worktree sessions accumulated
    forever.  Verify the new walker visits every project subdir and
    still respects the preserve-set across them.
    """
    import os
    import time

    fake_home = tmp_path
    monkeypatch.setenv("HOME", str(fake_home))

    projects = fake_home / ".claude" / "projects"
    old = time.time() - (48 * 3600)

    # Main workspace: one stale, one active
    main = projects / "-workspace"
    main.mkdir(parents=True)
    stale_main = main / "sess-stale-main"
    stale_main.mkdir()
    (stale_main / "transcript.jsonl").write_text("...")
    os.utime(stale_main, (old, old))

    active_main = main / "sess-active-main"
    active_main.mkdir()
    (active_main / "transcript.jsonl").write_text("...")
    os.utime(active_main, (old, old))

    # Worktree project: one stale (should be deleted)
    worktree = projects / "-workspace--worktrees-feat-foo"
    worktree.mkdir(parents=True)
    stale_wt = worktree / "sess-stale-wt"
    stale_wt.mkdir()
    (stale_wt / "transcript.jsonl").write_text("...")
    os.utime(stale_wt, (old, old))

    # Worktree project: one active (must be preserved)
    active_wt = worktree / "sess-active-wt"
    active_wt.mkdir()
    (active_wt / "transcript.jsonl").write_text("...")
    os.utime(active_wt, (old, old))

    # Two sessions registered in the store — these must survive cleanup
    executor._sessions.set_with_version(
        "chat-main", "sess-active-main", 1, branch=None,
    )
    executor._sessions.set_with_version(
        "chat-wt", "sess-active-wt", 1, branch="feat/foo",
    )

    counts = executor.cleanup_stale_sessions(max_age_hours=24)

    assert counts["projects"] == 2, (
        f"expected to clean stale-main + stale-wt = 2, got {counts}"
    )
    assert not stale_main.exists()
    assert not stale_wt.exists(), (
        "worktree-scoped stale session must be reaped — was untouched before fix"
    )
    assert active_main.exists()
    assert active_wt.exists(), (
        "active worktree session must be preserved across project dirs"
    )


# --- _resolve_resume_target (Codex fix: lock key follows effective branch) ──
#
# The earlier draft of this PR resolved the effective branch only inside
# _execute_inner, *after* _run_one had already acquired the per-worktree
# lock via _resolve_slot(req).  For a resumed Telegram turn with
# req.branch=None but a stored branch, the lock was keyed by "__main__"
# while execution ran in the stored branch's worktree — letting a
# concurrent Superpos task on the same branch trample the git tree.
# These tests pin the invariant: the effective branch drives both the
# lock key and the cwd.

def test_resolve_resume_target_superpos_passes_through_req_branch(executor):
    """Superpos requests skip the SessionStore — branch comes from the
    task payload, never from a chat-scoped session."""
    req = ExecutionRequest(
        prompt="do", chat_id="chat-1", source="superpos",
        superpos_task_id="t1", branch="feat/sp",
    )
    assert executor._resolve_resume_target(req) == (None, "feat/sp")


def test_resolve_resume_target_telegram_no_session_returns_req_branch(executor):
    req = ExecutionRequest(prompt="hi", chat_id="chat-1", source="telegram")
    assert executor._resolve_resume_target(req) == (None, None)


def test_resolve_resume_target_telegram_restores_stored_branch(executor):
    executor._persona_version = 3
    executor._sessions.set_with_version(
        "chat-1", "sess-pr607", 3, branch="feat/issues",
    )
    req = ExecutionRequest(prompt="Go", chat_id="chat-1", source="telegram")
    assert executor._resolve_resume_target(req) == ("sess-pr607", "feat/issues")


def test_resolve_resume_target_explicit_branch_wins_over_stored(executor):
    executor._persona_version = 3
    executor._sessions.set_with_version(
        "chat-1", "sess-old", 3, branch="feat/old",
    )
    req = ExecutionRequest(
        prompt="switch", chat_id="chat-1", source="telegram", branch="feat/new",
    )
    assert executor._resolve_resume_target(req) == ("sess-old", "feat/new")


def test_resolve_resume_target_drops_resume_on_persona_bump(executor):
    """Stale-persona session is cleared as a side effect — caller gets
    no resume and the entry no longer exists in the store."""
    executor._persona_version = 5
    executor._sessions.set_with_version(
        "chat-1", "sess-stale", 1, branch="feat/x",
    )
    req = ExecutionRequest(prompt="hi", chat_id="chat-1", source="telegram")
    assert executor._resolve_resume_target(req) == (None, None)
    assert executor._sessions.get_with_version("chat-1") is None


async def test_run_one_keys_worktree_lock_by_effective_branch(
    executor, mock_config,
):
    """The fix Codex flagged: a Telegram follow-up with req.branch=None
    that resumes on a stored branch must acquire the per-branch lock,
    not "__main__".  Two such turns targeting the same stored branch
    have to serialise on the worktree lock — otherwise a concurrent
    Superpos task on the same branch tramples the git tree.
    """
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"

    executor._persona_version = 1
    executor._sessions.set_with_version(
        "chat-1", "sess-1", 1, branch="feat/shared",
    )

    captured_slots: list[str] = []
    real_get_lock = executor._get_worktree_lock

    def trace_lock(slot: str):
        captured_slots.append(slot)
        return real_get_lock(slot)

    async def fake_execute(req, claim_expired, retries=3, *, pre_resolved=None):
        # Pre-resolved must carry the restored branch so cwd matches lock.
        assert pre_resolved == ("sess-1", "feat/shared")
        await asyncio.sleep(0)

    req = ExecutionRequest(
        prompt="Go", chat_id="chat-1", source="telegram",  # no branch
    )

    with patch.object(executor, "_get_worktree_lock", side_effect=trace_lock), \
         patch.object(executor, "_execute", side_effect=fake_execute), \
         patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True):
        await executor.queue.put(req)
        await executor.queue.get()  # mirror what run() would do
        await executor._run_one(req)

    assert captured_slots == ["/workspace/.worktrees/feat-shared"], (
        f"lock must be keyed by restored branch, not __main__: got {captured_slots}"
    )


async def test_resumed_telegram_serialises_with_explicit_branch_on_same_branch(
    executor, mock_config,
):
    """End-to-end concurrency guard: a resumed Telegram turn (branch
    restored from SessionStore) and a Superpos task with the same
    explicit branch share a worktree lock — they must NOT overlap.
    Pre-fix: lock would be "__main__" for the Telegram turn, the
    Superpos task would take the branch slot, and both ran at once.
    """
    mock_config.executor_worktree_isolation = True
    mock_config.executor_working_dir = "/workspace"
    mock_config.executor_max_parallel = 2  # let both into the semaphore

    executor._persona_version = 1
    executor._sessions.set_with_version(
        "chat-tg", "sess-r", 1, branch="feat/shared",
    )
    # The semaphore bound is read at __init__; rebuild it to match.
    executor._semaphore = asyncio.Semaphore(2)

    log_events: list[str] = []

    async def fake_execute_inner(req, streamer, retries, *, pre_resolved=None):
        log_events.append(f"start-{req.chat_id}")
        await asyncio.sleep(0.05)
        log_events.append(f"end-{req.chat_id}")

    req_tg = ExecutionRequest(
        prompt="Go", chat_id="chat-tg", source="telegram",  # no branch
    )
    req_sp = ExecutionRequest(
        prompt="cron", chat_id="chat-sp", source="superpos",
        superpos_task_id="sp-1", branch="feat/shared",
    )

    with patch.object(executor, "_execute_inner", fake_execute_inner), \
         patch("slim_agent_claude.claude_executor.is_git_repo", return_value=True), \
         patch("slim_agent_claude.claude_executor.TelegramStreamer") as MockStreamer:
        MockStreamer.return_value.start = AsyncMock()
        await executor.queue.put(req_tg)
        await executor.queue.put(req_sp)

        run_task = asyncio.create_task(executor.run())
        await asyncio.sleep(0.25)
        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass

    # Both ran (start + end recorded for each)
    assert {"start-chat-tg", "end-chat-tg", "start-chat-sp", "end-chat-sp"} <= set(log_events)
    # And they did NOT overlap — the earlier task ends before the later starts.
    starts = [i for i, e in enumerate(log_events) if e.startswith("start-")]
    first_end_idx = next(
        i for i, e in enumerate(log_events) if e.startswith("end-")
    )
    second_start_idx = starts[1]
    assert first_end_idx < second_start_idx, (
        f"resumed Telegram turn and Superpos task on same branch overlapped: "
        f"{log_events}"
    )
