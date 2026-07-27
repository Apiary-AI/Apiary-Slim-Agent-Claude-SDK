"""Unit tests for ${VAR} placeholder resolution in MCP server env blocks.

``collect_mcp_servers`` (agent-core) merges each module's inline ``mcp:`` block
verbatim, with no placeholder expansion. ``_resolve_mcp_env_placeholders`` is the
executor's seam that resolves bare ``${VAR}`` env values from ``os.environ`` at
launch time, dropping any server whose secret is unset rather than launching it
with a literal placeholder.

These tests hit the helper directly so they are non-vacuous: deleting the helper
(or its substitution/drop logic) fails them.
"""

import pytest

from superpos_agent_claude.claude_executor import _resolve_mcp_env_placeholders


def _pexels_servers():
    return {
        "pexels": {
            "type": "stdio",
            "command": "node",
            "args": ["/app/mcp-servers/pexels/src/index.js"],
            "env": {"PEXELS_API_KEY": "${PEXELS_API_KEY}"},
        }
    }


def test_placeholder_resolved_to_real_env_value(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "secret-abc123")

    resolved = _resolve_mcp_env_placeholders(_pexels_servers())

    assert "pexels" in resolved, "server must be kept when its secret is set"
    assert resolved["pexels"]["env"]["PEXELS_API_KEY"] == "secret-abc123"
    # The literal placeholder must never survive into the launched config.
    assert resolved["pexels"]["env"]["PEXELS_API_KEY"] != "${PEXELS_API_KEY}"


def test_server_dropped_when_env_var_unset(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)

    resolved = _resolve_mcp_env_placeholders(_pexels_servers())

    assert "pexels" not in resolved, (
        "server with an unresolved placeholder must be dropped, not launched "
        "with a literal ${...} value"
    )


def test_server_dropped_when_env_var_empty(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "")

    resolved = _resolve_mcp_env_placeholders(_pexels_servers())

    assert "pexels" not in resolved


def test_non_placeholder_values_untouched(monkeypatch):
    # Real config values (as minimax/web_search carry) must pass through
    # unchanged, and a $-containing var must NOT trigger substitution of a
    # non-placeholder value.
    monkeypatch.setenv("PEXELS_API_KEY", "unused")
    servers = {
        "web_search": {
            "type": "http",
            "url": "https://example.test/mcp",
            "env": {
                "API_KEY": "real-literal-key",
                "PARTIAL": "prefix-${PEXELS_API_KEY}",  # not a bare placeholder
            },
        }
    }

    resolved = _resolve_mcp_env_placeholders(servers)

    assert resolved["web_search"]["env"]["API_KEY"] == "real-literal-key"
    assert resolved["web_search"]["env"]["PARTIAL"] == "prefix-${PEXELS_API_KEY}"


def test_server_without_env_block_passes_through():
    servers = {"plain": {"type": "stdio", "command": "node"}}
    resolved = _resolve_mcp_env_placeholders(servers)
    assert resolved == servers


def test_input_not_mutated(monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "secret-abc123")
    servers = _pexels_servers()
    _resolve_mcp_env_placeholders(servers)
    # Original dict still holds the placeholder — helper returns a new dict.
    assert servers["pexels"]["env"]["PEXELS_API_KEY"] == "${PEXELS_API_KEY}"


@pytest.mark.parametrize(
    "value, expected_key",
    [
        ("${A}", "A"),
        ("${A_B}", "A_B"),
        ("${_x1}", "_x1"),
    ],
)
def test_various_valid_placeholder_names(monkeypatch, value, expected_key):
    monkeypatch.setenv(expected_key, "v")
    servers = {"s": {"env": {"K": value}}}
    resolved = _resolve_mcp_env_placeholders(servers)
    assert resolved["s"]["env"]["K"] == "v"
