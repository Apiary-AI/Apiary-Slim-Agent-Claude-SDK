"""_read_captured_stderr filters the --debug-to-stderr firehose to signal.

The CLI's debug output is ~99% per-turn bookkeeping (LSP diagnostics, hook
checks, tool dispatch); a raw byte-tail buries the exit-1 cause in noise.
These tests pin that the reader surfaces the API/stream/error/teardown
lines that actually explain a crash, while never losing information.
"""

from superpos_agent_claude.claude_executor import _read_captured_stderr


# A realistic slice of CLI --debug-to-stderr output around a mid-stream
# stall crash: noise bookkeeping interleaved with the signal lines.
_NOISE = "\n".join(
    f"2026-06-14T10:36:1{i}.000Z [DEBUG] LSP Diagnostics: Checking registry - 0 pending\n"
    f"2026-06-14T10:36:1{i}.001Z [DEBUG] Hooks: checkForNewResponses returning 0 responses\n"
    f"2026-06-14T10:36:1{i}.002Z [DEBUG] dispatch_end tool=Read outcome=ok durationMs=1"
    for i in range(6)
)
_SIGNAL = (
    "2026-06-14T10:36:16.956Z [DEBUG] [API REQUEST] /v1/messages model=claude-opus-4-8\n"
    "2026-06-14T10:36:19.912Z [DEBUG] Stream started - received first chunk\n"
    "2026-06-14T10:36:19.913Z [DEBUG] [API:timing] first byte after 2958ms\n"
    '2026-06-14T10:36:36.485Z [DEBUG] MCP server "claude.ai Gmail": Cleared connection cache for reconnection\n'
    "2026-06-14T10:36:36.490Z [DEBUG] Cleaned up session snapshot: /home/agent/.claude/x.sh"
)


def _write(tmp_path, text):
    p = tmp_path / "claude-stderr.log"
    p.write_text(text)
    return str(p)


def test_missing_and_empty_paths_return_empty(tmp_path):
    assert _read_captured_stderr(None) == ""
    assert _read_captured_stderr(_write(tmp_path, "")) == ""


def test_surfaces_signal_lines_and_drops_noise(tmp_path):
    path = _write(tmp_path, _NOISE + "\n" + _SIGNAL)
    out = _read_captured_stderr(path)

    # The lines that explain the crash are present...
    assert "[API REQUEST] /v1/messages" in out
    assert "Stream started" in out
    assert "first byte after 2958ms" in out
    assert "Cleared connection cache" in out
    # ...and the repeated per-turn bookkeeping is gone (a line or two of
    # raw context may survive in the always-kept exit tail, but the bulk
    # noise that floods the firehose must not).
    assert "LSP Diagnostics" not in out
    assert "checkForNewResponses" not in out


def test_exit_tail_preserved_even_when_pure_noise(tmp_path):
    # No signal lines at all → fall back to the raw trailing lines so we
    # never return empty when there was output.
    path = _write(tmp_path, _NOISE)
    out = _read_captured_stderr(path)
    assert out  # not empty
    # The last bookkeeping line is still shown as exit context.
    assert "dispatch_end" in out or "Hooks" in out


def test_error_keywords_are_treated_as_signal(tmp_path):
    text = _NOISE + "\n2026-06-14T10:36:40Z [ERROR] overloaded_error: 529\n" + _NOISE
    out = _read_captured_stderr(_write(tmp_path, text))
    assert "overloaded_error: 529" in out


def test_result_is_clamped_to_max_bytes(tmp_path):
    big = "\n".join(f"[API:timing] request {i} first byte after 10ms" for i in range(5000))
    out = _read_captured_stderr(_write(tmp_path, big), max_bytes=2048)
    assert len(out) <= 2048 + len("…(truncated)…\n")
    # Still ends with the most recent (highest-numbered) signal lines.
    assert "request 4999" in out
