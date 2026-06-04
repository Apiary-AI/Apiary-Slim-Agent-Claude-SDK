"""Backend-aware tooling: shim (MiniMax) vs native Anthropic."""

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


# --- executor tool wiring ---

def test_native_keeps_hosted_tools_and_no_minimax_mcp(executor):
    """Default (native Anthropic) — hosted tools stay, no MiniMax MCP."""
    opts = executor._build_options()
    assert opts.disallowed_tools == []
    assert "minimax" not in (opts.mcp_servers or {})


def test_shim_disables_hosted_tools_and_adds_minimax_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    mock_config.is_native_anthropic = False
    mock_config.minimax_api_key = "mm-key"
    mock_config.minimax_api_host = "https://api.minimax.io"
    mock_config.anthropic_base_url = "https://api.minimax.io/anthropic"

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    assert opts.disallowed_tools == ["WebSearch", "WebFetch"]
    assert "minimax" in opts.mcp_servers
    mm = opts.mcp_servers["minimax"]
    assert mm["command"] == "uvx"
    assert mm["args"] == ["minimax-coding-plan-mcp", "-y"]
    assert mm["env"]["MINIMAX_API_KEY"] == "mm-key"
    assert mm["env"]["MINIMAX_API_HOST"] == "https://api.minimax.io"
    # The model is told to use the MCP tool for search.
    assert "web_search" in (opts.append_system_prompt or "")


def test_shim_without_key_disables_tools_but_adds_no_mcp(
    mock_config, mock_runtime, mock_superpos, mock_gateway,
):
    mock_config.is_native_anthropic = False
    mock_config.minimax_api_key = ""
    mock_config.anthropic_base_url = "https://api.minimax.io/anthropic"

    ex = ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)
    opts = ex._build_options()

    # Dead hosted tools are still disabled even without a search MCP configured.
    assert opts.disallowed_tools == ["WebSearch", "WebFetch"]
    assert "minimax" not in (opts.mcp_servers or {})
