"""Tests for ClaudeConfig.from_env() defaults and ClaudeRuntimeConfig.KNOWN_MODELS.

These tests pin the boot-time default model and the /model allow-list to
``claude-opus-4-8`` so that the change cannot regress silently on a future
config refactor.
"""

from __future__ import annotations

import pytest

from superpos_agent_claude.config import ClaudeConfig
from superpos_agent_claude.runtime_config import ClaudeRuntimeConfig


class TestClaudeConfigDefaults:
    """``ClaudeConfig.from_env()`` must default ``claude_model`` to opus-4-8."""

    def test_claude_model_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_MODEL", raising=False)

        cfg = ClaudeConfig.from_env()

        assert cfg.claude_model == "claude-opus-4-8"

    def test_claude_model_dataclass_default(self):
        # The dataclass-level default also needs to stay in sync with from_env,
        # in case future call sites instantiate ClaudeConfig() directly.
        assert ClaudeConfig.claude_model == "claude-opus-4-8"

    def test_claude_model_env_override_still_wins(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-sonnet-4-6")

        cfg = ClaudeConfig.from_env()

        assert cfg.claude_model == "claude-sonnet-4-6"


class TestClaudeRuntimeKnownModels:
    """``/model list`` must advertise ``claude-opus-4-8`` and accept it at runtime."""

    def test_opus_4_8_in_known_models(self):
        assert "claude-opus-4-8" in ClaudeRuntimeConfig.KNOWN_MODELS

    def test_set_model_accepts_opus_4_8(self, tmp_path):
        rc = ClaudeRuntimeConfig(
            model="claude-opus-4-7",
            effort="high",
            path=str(tmp_path / "runtime_config.json"),
        )

        rc.set_model("claude-opus-4-8")

        assert rc.model == "claude-opus-4-8"
