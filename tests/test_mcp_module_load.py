"""MCP-4 smoke/regression test: prove an mcp-bearing module is actually loaded
into the Claude runtime's MCP server config.

This pins the loading seam in ``claude_executor.py``:

- ``ClaudeExecutor.__init__`` (~L473-483) calls
  ``collect_mcp_servers(discover_modules(config.modules_dir))`` into
  ``self._mcp``, then merges the in-process ask server.
- ``_build_options`` (~L1639-1654) assembles ``mcp_servers`` from ``self._mcp``
  and sets ``opts["mcp_servers"]`` when non-empty.

The assertions are deliberately concrete (server name + url): if the module's
MCP server were dropped anywhere along that path, this test fails rather than
passing vacuously.
"""

import textwrap

from superpos_agent_claude.claude_executor import ClaudeExecutor, _ASK_MCP_SERVER


# Canonical MCP-4 reference module: a remote-HTTP MCP server. Matches the other
# MCP-4 PRs exactly.
_MODULE_YAML = textwrap.dedent(
    """\
    description: "Example remote-HTTP MCP module (MCP-4 reference)."
    env: []
    mcp:
      example-remote:
        url: "https://mcp.example.com/sse"
    """
)

_SERVER_NAME = "example-remote"
_SERVER_URL = "https://mcp.example.com/sse"


def _write_example_module(modules_dir):
    """Create ``<modules_dir>/example-remote-mcp/module.yaml`` and return the dir."""
    mod = modules_dir / "example-remote-mcp"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "module.yaml").write_text(_MODULE_YAML)
    return modules_dir


def _make_executor(mock_config, mock_runtime, mock_superpos, mock_gateway, modules_dir):
    mock_config.modules_dir = str(modules_dir)
    return ClaudeExecutor(mock_config, mock_runtime, mock_superpos, mock_gateway)


def test_module_mcp_server_loaded_into_executor(
    tmp_path, mock_config, mock_runtime, mock_superpos, mock_gateway
):
    """The executor's __init__ loading seam must surface the module's MCP
    server (name + url) in ``self._mcp``."""
    modules_dir = _write_example_module(tmp_path / "modules")
    ex = _make_executor(mock_config, mock_runtime, mock_superpos, mock_gateway, modules_dir)

    assert _SERVER_NAME in ex._mcp, (
        f"module MCP server {_SERVER_NAME!r} was not loaded into self._mcp; "
        f"got keys={sorted(ex._mcp)}"
    )
    assert ex._mcp[_SERVER_NAME] == {"url": _SERVER_URL}


def test_module_mcp_server_in_built_runtime_options(
    tmp_path, mock_config, mock_runtime, mock_superpos, mock_gateway
):
    """The end-to-end runtime seam: after _build_options assembles the SDK
    options, the module's MCP server (name + url) must be present in the
    runtime's ``mcp_servers`` config that the Claude CLI is launched with."""
    modules_dir = _write_example_module(tmp_path / "modules")
    ex = _make_executor(mock_config, mock_runtime, mock_superpos, mock_gateway, modules_dir)

    opts = ex._build_options()
    mcp_servers = opts.mcp_servers or {}

    # Non-vacuous: the concrete module server + url survived assembly.
    assert _SERVER_NAME in mcp_servers, (
        f"module MCP server {_SERVER_NAME!r} missing from built runtime "
        f"mcp_servers; got keys={sorted(mcp_servers)}"
    )
    assert mcp_servers[_SERVER_NAME] == {"url": _SERVER_URL}


def test_module_mcp_server_survives_background_no_ask_path(
    tmp_path, mock_config, mock_runtime, mock_superpos, mock_gateway
):
    """On the background (enable_ask=False) path the in-process ask server is
    stripped, but a real module-provided MCP server must still be loaded — it
    is not "optional" and must reach the runtime regardless of the ask path."""
    modules_dir = _write_example_module(tmp_path / "modules")
    ex = _make_executor(mock_config, mock_runtime, mock_superpos, mock_gateway, modules_dir)

    opts = ex._build_options(enable_ask=False)
    mcp_servers = opts.mcp_servers or {}

    # The ask server is dropped on this path...
    assert _ASK_MCP_SERVER not in mcp_servers
    # ...but the module's MCP server is still there with its url.
    assert mcp_servers.get(_SERVER_NAME) == {"url": _SERVER_URL}
