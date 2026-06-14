"""Backend-aware tooling: shims (MiniMax, Kimi, custom) vs native Anthropic."""

import pytest

from superpos_agent_claude.config import ClaudeConfig
from superpos_agent_claude.claude_executor import ClaudeExecutor


# --- backend detection ---

@pytest.mark.parametrize(
    "base_url, native",
    [
        ("", True),
        ("https://api.anthropic.com", True),
        ("https://API.ANTHROPIC.COM/v1", True),
        ("https://api.minimax.io/anthropic", False),
        ("https://api.minimaxi.com/anthropic", False),
        ("https://api.kimi.com/coding/", False),
        ("https://my-gateway.internal/anthropic", False),
        # Substring-match traps: a non-Anthropic host that merely *contains*
        # the string "anthropic.com" in the path, query, or as a deceptive
        # subdomain must NOT be classified as native.
        ("https://my-proxy.com/anthropic.com-shim", False),
        ("https://anthropic.com.evil.com/v1", False),
        ("https://gateway.example.com/?backend=anthropic.com", False),
        ("https://not-anthropic.com/v1", False),
        # Subdomains of anthropic.com (and a scheme-less form) are native.
        ("https://foo.anthropic.com/v1", True),
        ("api.anthropic.com", True),
    ],
)
def test_is_native_anthropic(base_url, native):
    assert ClaudeConfig(anthropic_base_url=base_url).is_native_anthropic is native


def test_from_env_reads_shim_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")
    monkeypatch.setenv("MINIMAX_API_KEY", "mm-key")
    cfg = ClaudeConfig.from_env()
    assert cfg.anthropic_base_url == "https://api.minimax.io/anthropic"
    assert cfg.minimax_api_key == "mm-key"
    assert cfg.is_native_anthropic is False


def test_from_env_reads_web_search_mcp(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")
    monkeypatch.setenv(
        "WEB_SEARCH_MCP", '{"command": "npx", "args": ["-y", "tavily-mcp"]}'
    )
    cfg = ClaudeConfig.from_env()
    assert cfg.web_search_mcp == '{"command": "npx", "args": ["-y", "tavily-mcp"]}'
    assert cfg.is_native_anthropic is False


# --- executor tool wiring ---

def test_native_keeps_hosted_tools_and_no_search_mcp(executor):
    """Default (native Anthropic) — hosted tools stay, no replacement MCP."""
    opts = executor._build_options()
    # AskUserQuestion is always disabled (steered to the MCP tool); the
    # hosted web tools stay enabled on native Anthropic.
    assert opts.disallowed_tools == ["AskUserQuestion"]
    assert "minimax" not in (opts.mcp_servers or {})
    assert "web_search" not in (opts.mcp_servers or {})


def test_native_ignores_web_search_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    """On native Anthropic the hosted tools win — WEB_SEARCH_MCP is ignored."""
    mock_config.web_search_mcp = '{"command": "npx", "args": ["-y", "tavily-mcp"]}'

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    assert opts.disallowed_tools == ["AskUserQuestion"]
    assert "web_search" not in (opts.mcp_servers or {})


def test_shim_disables_hosted_tools_and_adds_minimax_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    mock_config.is_native_anthropic = False
    mock_config.minimax_api_key = "mm-key"
    mock_config.minimax_api_host = "https://api.minimax.io"
    mock_config.anthropic_base_url = "https://api.minimax.io/anthropic"

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    assert opts.disallowed_tools == ["AskUserQuestion", "WebSearch", "WebFetch"]
    assert "minimax" in opts.mcp_servers
    mm = opts.mcp_servers["minimax"]
    assert mm["command"] == "uvx"
    assert mm["args"] == ["minimax-coding-plan-mcp", "-y"]
    assert mm["env"]["MINIMAX_API_KEY"] == "mm-key"
    assert mm["env"]["MINIMAX_API_HOST"] == "https://api.minimax.io"
    # The model is told to use the MCP tool for search.
    assert "web_search" in (opts.append_system_prompt or "")


def test_shim_with_custom_search_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    """A non-MiniMax shim (e.g. Kimi) gets the configurable search MCP."""
    mock_config.is_native_anthropic = False
    mock_config.anthropic_base_url = "https://api.kimi.com/coding/"
    mock_config.web_search_mcp = (
        '{"command": "npx", "args": ["-y", "tavily-mcp"],'
        ' "env": {"TAVILY_API_KEY": "tvly-key"}}'
    )

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    assert opts.disallowed_tools == ["AskUserQuestion", "WebSearch", "WebFetch"]
    assert "minimax" not in opts.mcp_servers
    ws = opts.mcp_servers["web_search"]
    assert ws["type"] == "stdio"  # defaulted when absent from the JSON
    assert ws["command"] == "npx"
    assert ws["args"] == ["-y", "tavily-mcp"]
    assert ws["env"]["TAVILY_API_KEY"] == "tvly-key"
    # The model is pointed at the replacement server.
    assert "web_search" in (opts.append_system_prompt or "")


def test_custom_search_mcp_wins_over_minimax(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    """Explicit WEB_SEARCH_MCP beats the implicit MiniMax default."""
    mock_config.is_native_anthropic = False
    mock_config.anthropic_base_url = "https://api.kimi.com/coding/"
    mock_config.minimax_api_key = "mm-key"
    mock_config.web_search_mcp = '{"command": "npx", "args": ["-y", "exa-mcp-server"]}'

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    assert "web_search" in opts.mcp_servers
    assert "minimax" not in opts.mcp_servers


@pytest.mark.parametrize(
    "bad_value",
    [
        "not json at all",
        '["command", "npx"]',  # a list, not an object
        '{"args": ["-y", "tavily-mcp"]}',  # missing "command"
    ],
)
def test_invalid_web_search_mcp_adds_no_server(
    bad_value, mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    mock_config.is_native_anthropic = False
    mock_config.anthropic_base_url = "https://api.kimi.com/coding/"
    mock_config.web_search_mcp = bad_value

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    # Dead hosted tools are still disabled; the broken config is dropped.
    assert opts.disallowed_tools == ["AskUserQuestion", "WebSearch", "WebFetch"]
    assert "web_search" not in (opts.mcp_servers or {})


def test_shim_without_key_disables_tools_but_adds_no_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    mock_config.is_native_anthropic = False
    mock_config.anthropic_base_url = "https://api.minimax.io/anthropic"

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    # Dead hosted tools are still disabled even without a search MCP configured.
    assert opts.disallowed_tools == ["AskUserQuestion", "WebSearch", "WebFetch"]
    assert "minimax" not in (opts.mcp_servers or {})
    assert "web_search" not in (opts.mcp_servers or {})


# --- CLI crash diagnostics ---

def test_build_options_passes_debug_to_stderr(executor):
    """--debug-to-stderr must be on so a CLI crash leaves a real error in
    the captured stderr instead of "(no stderr captured)"."""
    opts = executor._build_options()
    # A None value renders as a bare boolean flag (--debug-to-stderr) in the SDK.
    assert opts.extra_args.get("debug-to-stderr", "MISSING") is None
    # The pre-existing effort arg must survive alongside it.
    assert opts.extra_args["effort"] == executor._runtime.effort
    # debug_stderr must be None (not the sys.stderr default).  The SDK only
    # forwards stderr to open_process when debug-to-stderr is set AND
    # debug_stderr is truthy; leaving it at sys.stderr would route crash
    # output to the container stderr and defeat the open_process capture.
    assert opts.debug_stderr is None
