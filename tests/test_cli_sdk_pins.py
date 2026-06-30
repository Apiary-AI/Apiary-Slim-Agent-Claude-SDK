"""Version-skew guard for the Claude CLI ⇄ Python SDK pair (issue #40).

The in-process ``superpos_ask`` MCP server + streaming control protocol in
``claude_executor.py`` speak a specific control-protocol version. That version
is determined jointly by the bundled Claude CLI (installed in ``Dockerfile``)
and the Python SDK (pinned in ``requirements.txt`` / ``pyproject.toml``).

When the CLI was installed unpinned (``npm install -g @anthropic-ai/claude-code``)
a rebuild silently pulled CLI ``latest`` (2.1.x), whose handshake the pinned
SDK ``claude-code-sdk==0.0.25`` no longer survived. The CLI exited before the
``init`` system message → the executor treated it as a pre-init crash with
optional MCP present → stripped ask/search and degraded on every message.

These tests assert that both ends stay pinned to the known-compatible pair so
the skew cannot silently regress on a future edit or rebuild. A real boot
needs OAuth + network and can't run in CI, so we verify the *contract* instead.

Bump BOTH constants together (and only to a pair you've verified boots to a
clean ``init``) when intentionally moving the pin.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The known-compatible pair. claude-code-sdk 0.0.25 (the last release under the
# old package name) shipped 2025-09-29, alongside the CLI 2.0.x line; CLI 2.0.14
# is a settled patch in that line verified against the streaming control
# protocol the SDK implements. Keep these two in lockstep.
EXPECTED_CLI_VERSION = "2.0.14"
EXPECTED_SDK_SPEC = "claude-code-sdk==0.0.25"


def test_dockerfile_pins_cli_version():
    """The CLI install line must pin an exact version, not float on latest."""
    text = DOCKERFILE.read_text()
    match = re.search(
        r"npm install -g @anthropic-ai/claude-code@(\S+)", text
    )
    assert match, (
        "Dockerfile must install @anthropic-ai/claude-code pinned to an exact "
        "version (npm install -g @anthropic-ai/claude-code@<version>); an "
        "unpinned install pulls CLI `latest` and reintroduces the issue-#40 skew."
    )
    assert match.group(1) == EXPECTED_CLI_VERSION, (
        f"CLI pinned to {match.group(1)!r}, expected {EXPECTED_CLI_VERSION!r}. "
        "If this is an intentional bump, update EXPECTED_CLI_VERSION *and* the "
        "SDK pin together to a pair verified to boot to a clean init."
    )


def test_requirements_pins_sdk_exactly():
    """requirements.txt must pin the SDK exactly (no open-ended >=)."""
    text = REQUIREMENTS.read_text()
    assert EXPECTED_SDK_SPEC in text, (
        f"requirements.txt must contain an exact pin {EXPECTED_SDK_SPEC!r}; "
        "an open-ended >= drifts the SDK away from the pinned CLI."
    )
    # Guard against a stray loose specifier sneaking back in.
    loose = re.search(r"claude-code-sdk\s*>=", text)
    assert loose is None, (
        "claude-code-sdk must be pinned with == in requirements.txt, not >=."
    )


def test_pyproject_pins_sdk_exactly():
    """pyproject.toml must carry the same exact SDK pin as requirements.txt."""
    text = PYPROJECT.read_text()
    assert EXPECTED_SDK_SPEC in text, (
        f"pyproject.toml must contain an exact pin {EXPECTED_SDK_SPEC!r} to "
        "match requirements.txt and the Dockerfile CLI pin."
    )
    loose = re.search(r"claude-code-sdk\s*>=", text)
    assert loose is None, (
        "claude-code-sdk must be pinned with == in pyproject.toml, not >=."
    )
