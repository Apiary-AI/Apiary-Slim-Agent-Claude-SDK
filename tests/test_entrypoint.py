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


class TestModuleSetupPaths:
    """The module_setup block must use $WORKING_DIR so it agrees with the
    downstream sync block. If module_setup writes to /workspace/.claude/modules
    but sync scans $WORKING_DIR/.claude/modules, an overridden CLAUDE_WORKING_DIR
    silently loses the installed-module context."""

    def _module_setup_block(self) -> str:
        text = _read_entrypoint()
        match = re.search(
            r"(python3 -m superpos_agent_core\.module_setup\b.*?)(?:\n(?!\s)|\Z)",
            text,
            re.DOTALL,
        )
        assert match, "Could not locate module_setup invocation in entrypoint.sh"
        return match.group(1)

    def test_modules_dir_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/.claude/modules"' in block

    def test_agents_md_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/CLAUDE.md"' in block

    def test_bin_dir_uses_working_dir(self):
        block = self._module_setup_block()
        assert '"$WORKING_DIR/.claude/modules-bin"' in block

    def test_no_hardcoded_workspace_in_module_setup_block(self):
        block = self._module_setup_block()
        cleaned = block.replace("$WORKING_DIR", "")
        assert "/workspace" not in cleaned, (
            "module_setup block still contains hard-coded /workspace paths"
        )


class TestPathContractAgreement:
    """module_setup and sync_sub_agents must point at the same module/skill
    roots, otherwise an overridden CLAUDE_WORKING_DIR causes the sync step to
    scan an empty directory."""

    def test_modules_dir_matches_between_blocks(self):
        text = _read_entrypoint()
        setup_match = re.search(
            r"--modules-dir\s+(\S+)\s*\\?\s*\n[^\n]*--agents-md", text
        )
        sync_match = re.search(
            r"sync_sub_agents\.py\b.*?--modules-dir\s+(\S+)", text, re.DOTALL
        )
        assert setup_match and sync_match
        assert setup_match.group(1) == sync_match.group(1), (
            "module_setup --modules-dir and sync_sub_agents --modules-dir "
            "must agree so an overridden CLAUDE_WORKING_DIR keeps them aligned"
        )
