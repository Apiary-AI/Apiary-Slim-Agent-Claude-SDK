import time
from unittest.mock import AsyncMock, patch

from src.telegram_streamer import TelegramStreamer, _humanize_tool, md_to_telegram


# --- md_to_telegram ---

def test_md_escapes_special_chars():
    assert md_to_telegram("hello_world.txt") == "hello\\_world\\.txt"

def test_md_heading_becomes_bold():
    assert md_to_telegram("## Hello World") == "*Hello World*"

def test_md_bold_becomes_single_star():
    assert md_to_telegram("some **text** here") == "some *text* here"

def test_md_code_block_content_not_escaped():
    result = md_to_telegram("```\nx_y = 1\n```")
    assert "x_y" in result
    assert "\\_" not in result

def test_md_inline_code_not_escaped():
    result = md_to_telegram("`x_y`")
    assert "x_y" in result
    assert "\\_" not in result

def test_md_escapes_outside_code_but_not_inside():
    result = md_to_telegram("file.txt `x_y` end.here")
    assert "file\\.txt" in result
    assert "end\\.here" in result
    assert "x_y" in result   # inside inline code — not escaped
    assert "\\_" not in result


# --- _humanize_tool ---

def test_humanize_bash_strips_at_double_ampersand():
    assert _humanize_tool("Bash", {"command": "cd /tmp && ls"}) == "Running command: cd /tmp"

def test_humanize_bash_strips_at_pipe():
    assert _humanize_tool("Bash", {"command": "git log | head -5"}) == "Running command: git log"

def test_humanize_read_extracts_filename():
    assert _humanize_tool("Read", {"file_path": "/some/path/file.py"}) == "Reading: file.py"

def test_humanize_glob_uses_pattern():
    assert _humanize_tool("Glob", {"pattern": "**/*.py"}) == "Searching files: **/*.py"

def test_humanize_grep_uses_pattern():
    assert _humanize_tool("Grep", {"pattern": "def foo"}) == "Searching code: def foo"

def test_humanize_websearch_uses_query():
    assert _humanize_tool("WebSearch", {"query": "python asyncio"}) == "Searching the web: python asyncio"

def test_humanize_webfetch_uses_url():
    assert _humanize_tool("WebFetch", {"url": "https://example.com"}) == "Fetching page: https://example.com"

def test_humanize_agent_uses_description_over_prompt():
    result = _humanize_tool("Agent", {"description": "Run tests", "prompt": "other"})
    assert result == "Running sub-agent: Run tests"

def test_humanize_agent_falls_back_to_prompt():
    assert _humanize_tool("Agent", {"prompt": "Do thing"}) == "Running sub-agent: Do thing"

def test_humanize_unknown_tool_uses_using_prefix():
    assert _humanize_tool("MyCustomTool", {}) == "Using MyCustomTool"

def test_humanize_no_detail_returns_label_only():
    assert _humanize_tool("Read", {}) == "Reading"

def test_humanize_long_detail_truncated_to_60():
    result = _humanize_tool("WebSearch", {"query": "a" * 80})
    detail = result.split(": ", 1)[1]
    assert detail == "a" * 57 + "..."
    assert len(detail) == 60


# --- _format_elapsed ---

def test_format_elapsed_under_60s():
    streamer = TelegramStreamer(AsyncMock(), "123")
    streamer._status_started = 1000.0
    with patch("src.telegram_streamer.time") as mock_time:
        mock_time.monotonic.return_value = 1045.0
        assert streamer._format_elapsed() == "45s"

def test_format_elapsed_over_60s():
    streamer = TelegramStreamer(AsyncMock(), "123")
    streamer._status_started = 1000.0
    with patch("src.telegram_streamer.time") as mock_time:
        mock_time.monotonic.return_value = 1090.0
        assert streamer._format_elapsed() == "1m 30s"

def test_format_elapsed_zero_seconds_zero_padded():
    streamer = TelegramStreamer(AsyncMock(), "123")
    streamer._status_started = 1000.0
    with patch("src.telegram_streamer.time") as mock_time:
        mock_time.monotonic.return_value = 1060.0
        assert streamer._format_elapsed() == "1m 00s"
