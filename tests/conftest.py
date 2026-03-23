import pytest
from unittest.mock import AsyncMock, MagicMock

from src.config import Config
from src.claude_executor import ClaudeExecutor


@pytest.fixture
def mock_config():
    cfg = MagicMock(spec=Config)
    cfg.claude_model = "claude-opus-4-5"
    cfg.claude_max_turns = 5
    cfg.claude_working_dir = "/tmp"
    cfg.apiary_poll_interval = 1
    cfg.telegram_chat_id = "123"
    cfg.claude_max_parallel = 3
    return cfg


@pytest.fixture
def mock_apiary():
    a = AsyncMock()
    a.update_progress = AsyncMock()
    a.poll_tasks = AsyncMock(return_value=[])
    a.claim_task = AsyncMock()
    a.complete_task = AsyncMock()
    a.fail_task = AsyncMock()
    a.heartbeat = AsyncMock()
    a.update_status = AsyncMock()
    return a


@pytest.fixture
def mock_gateway():
    gw = AsyncMock()
    gw.send_message = AsyncMock()
    gw.edit_message_text = AsyncMock()
    gw.delete_message = AsyncMock()
    gw.send_chat_action = AsyncMock()
    return gw


@pytest.fixture
def executor(mock_config, mock_apiary, mock_gateway):
    return ClaudeExecutor(mock_config, mock_apiary, mock_gateway)


@pytest.fixture
def executor_with_persona(mock_config, mock_apiary, mock_gateway):
    return ClaudeExecutor(mock_config, mock_apiary, mock_gateway, persona="You are a helpful assistant.")
