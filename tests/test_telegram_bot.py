import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.claude_executor import ClaudeExecutor, ExecutionRequest
from src.config import Config


def _make_update(text: str):
    """Build a minimal fake Telegram Update with the given message text."""
    update = MagicMock()
    update.effective_user.id = 42
    update.effective_chat.id = 99
    update.message.text = text
    return update


@pytest.fixture
def mock_executor():
    executor = MagicMock(spec=ClaudeExecutor)
    executor.queue = AsyncMock()
    executor.queue.put = AsyncMock()
    executor.pending = 0
    return executor


@pytest.fixture
def mock_cfg():
    cfg = MagicMock(spec=Config)
    cfg.telegram_allowed_users = []  # empty = allow all
    cfg.telegram_bot_token = "tok"
    cfg.claude_worktree_isolation = False
    cfg.claude_working_dir = "/workspace"
    return cfg


async def _invoke_handle_message(update, mock_executor, mock_cfg):
    """Register handlers via run_telegram_bot and extract handle_message, then call it."""
    from telegram.ext import Application

    captured_handler = {}

    class FakeApp:
        def add_handler(self, handler):
            # Capture the last non-command handler (handle_message)
            if hasattr(handler, "callback") and handler.callback.__name__ == "handle_message":
                captured_handler["fn"] = handler.callback

        async def initialize(self): pass
        async def start(self): pass
        updater = MagicMock()
        updater.start_polling = AsyncMock()

    fake_app = FakeApp()

    # Import and call run_telegram_bot just long enough to register handlers
    import asyncio
    from src.telegram_bot import run_telegram_bot

    task = asyncio.create_task(run_telegram_bot(fake_app, mock_executor, mock_cfg))
    # Give the coroutine enough time to register handlers (it blocks on asyncio.sleep)
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass

    fn = captured_handler.get("fn")
    assert fn is not None, "handle_message was not registered"
    await fn(update, None)


# --- --branch flag parsing ---

async def test_handle_message_parses_branch_flag(mock_executor, mock_cfg):
    update = _make_update("--branch feature/login implement the login screen")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    mock_executor.queue.put.assert_called_once()
    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch == "feature/login"
    assert req.prompt == "implement the login screen"
    assert req.source == "telegram"


async def test_handle_message_no_branch_flag(mock_executor, mock_cfg):
    update = _make_update("what files are in the project?")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    mock_executor.queue.put.assert_called_once()
    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch is None
    assert req.prompt == "what files are in the project?"
    assert req.source == "telegram"


# --- PR branch auto-resolution ---

@patch("src.telegram_bot._resolve_pr_branch", new_callable=AsyncMock)
async def test_handle_message_resolves_pr_branch(mock_resolve, mock_executor, mock_cfg):
    mock_cfg.claude_worktree_isolation = True
    mock_resolve.return_value = "feature/doc-locking"
    update = _make_update("check the feedback on PR #200")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    mock_resolve.assert_called_once_with(200, "/workspace")
    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch == "feature/doc-locking"
    assert req.prompt == "check the feedback on PR #200"


@patch("src.telegram_bot._resolve_pr_branch", new_callable=AsyncMock)
async def test_handle_message_pr_resolve_returns_none(mock_resolve, mock_executor, mock_cfg):
    mock_cfg.claude_worktree_isolation = True
    mock_resolve.return_value = None
    update = _make_update("look at #999")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch is None


@patch("src.telegram_bot._resolve_pr_branch", new_callable=AsyncMock)
async def test_handle_message_skips_resolve_when_isolation_off(mock_resolve, mock_executor, mock_cfg):
    mock_cfg.claude_worktree_isolation = False
    update = _make_update("fix PR #100")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    mock_resolve.assert_not_called()
    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch is None


@patch("src.telegram_bot._resolve_pr_branch", new_callable=AsyncMock)
async def test_handle_message_explicit_branch_skips_resolve(mock_resolve, mock_executor, mock_cfg):
    mock_cfg.claude_worktree_isolation = True
    update = _make_update("--branch my-branch fix PR #100")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    mock_resolve.assert_not_called()
    req: ExecutionRequest = mock_executor.queue.put.call_args[0][0]
    assert req.branch == "my-branch"


@patch("src.telegram_bot._resolve_pr_branch", new_callable=AsyncMock)
async def test_handle_message_resolves_first_pr_reference(mock_resolve, mock_executor, mock_cfg):
    mock_cfg.claude_worktree_isolation = True
    mock_resolve.return_value = "fix/auth"
    update = _make_update("compare #50 and #51")

    await _invoke_handle_message(update, mock_executor, mock_cfg)

    # Should resolve the first PR reference found
    mock_resolve.assert_called_once_with(50, "/workspace")


# --- _PR_REF_RE pattern ---

def test_pr_ref_regex_matches_various_formats():
    from src.telegram_bot import _PR_REF_RE

    assert _PR_REF_RE.search("PR #200").group(1) == "200"
    assert _PR_REF_RE.search("pr #42").group(1) == "42"
    assert _PR_REF_RE.search("PR#100").group(1) == "100"
    assert _PR_REF_RE.search("check #55 please").group(1) == "55"
    assert _PR_REF_RE.search("no pr here") is None


# --- _resolve_pr_branch ---

@patch("src.telegram_bot.subprocess")
async def test_resolve_pr_branch_success(mock_subprocess):
    from src.telegram_bot import _resolve_pr_branch

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "feature/doc-locking\n"
    mock_subprocess.run.return_value = mock_result

    branch = await _resolve_pr_branch(200, "/workspace/repo")

    assert branch == "feature/doc-locking"


@patch("src.telegram_bot.subprocess")
async def test_resolve_pr_branch_gh_fails(mock_subprocess):
    from src.telegram_bot import _resolve_pr_branch

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "not found"
    mock_result.stdout = ""
    mock_subprocess.run.return_value = mock_result

    branch = await _resolve_pr_branch(999, "/workspace/repo")

    assert branch is None
