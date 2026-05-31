"""Tests for entrypoint.sh — verify boot-time paths honour CLAUDE_WORKING_DIR."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "entrypoint.sh"


def _read_entrypoint() -> str:
    return ENTRYPOINT.read_text()


class TestWorkingDirVariable:
    """WORKING_DIR must be derived from CLAUDE_WORKING_DIR with a /workspace default."""

    def test_working_dir_defined(self):
        text = _read_entrypoint()
        assert re.search(
            r'WORKING_DIR="\$\{CLAUDE_WORKING_DIR:-/workspace\}"', text
        ), "entrypoint.sh must define WORKING_DIR from CLAUDE_WORKING_DIR"


class TestSubAgentSyncPaths:
    """The sub-agent sync block must use $WORKING_DIR, not hard-coded /workspace."""

    def _sync_block(self) -> str:
        """Return only the sync_sub_agents invocation block."""
        text = _read_entrypoint()
        match = re.search(
            r"(python3 /app/src/sync_sub_agents\.py\b.*?)(?:\n(?!\s)|\Z)",
            text,
            re.DOTALL,
        )
        assert match, "Could not locate sync_sub_agents.py invocation in entrypoint.sh"
        return match.group(1)

    def test_subagents_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/subagents"' in block

    def test_modules_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/modules"' in block

    def test_skills_dir_uses_working_dir(self):
        block = self._sync_block()
        assert '"$WORKING_DIR/.claude/skills"' in block

    def test_no_hardcoded_workspace_in_sync_block(self):
        block = self._sync_block()
        # After stripping the WORKING_DIR references there should be no
        # remaining /workspace paths in the sync block.
        cleaned = block.replace("$WORKING_DIR", "")
        assert "/workspace" not in cleaned, (
            "Sub-agent sync block still contains hard-coded /workspace paths"
        )
