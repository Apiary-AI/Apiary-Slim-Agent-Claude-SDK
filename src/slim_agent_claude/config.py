"""Claude-specific config: extends BaseConfig with Anthropic API key + model knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass

from slim_agent_core import BaseConfig


@dataclass
class ClaudeConfig(BaseConfig):
    """Adds Claude-specific knobs on top of the universal BaseConfig."""

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-6"
    claude_effort: str = "high"
    claude_max_budget_usd: float = 5.0

    def __post_init__(self) -> None:
        if not self.executor_kind or self.executor_kind == "generic":
            self.executor_kind = "claude"
        super().__post_init__()

    @classmethod
    def from_env(cls) -> "ClaudeConfig":
        base = cls._base_env_kwargs()

        # Honour the legacy CLAUDE_* env vars in addition to the generic ones,
        # so existing .env files keep working after the port.
        working_dir = os.environ.get("CLAUDE_WORKING_DIR")
        if working_dir:
            base["executor_working_dir"] = working_dir
            isolation_env = os.environ.get("CLAUDE_WORKTREE_ISOLATION")
            if isolation_env is not None:
                base["executor_worktree_isolation"] = (
                    isolation_env.lower() not in ("0", "false", "no")
                )
            else:
                base["executor_worktree_isolation"] = os.path.isdir(
                    os.path.join(working_dir, ".git")
                )
        if os.environ.get("CLAUDE_MAX_PARALLEL"):
            base["executor_max_parallel"] = int(os.environ["CLAUDE_MAX_PARALLEL"])
        if os.environ.get("CLAUDE_MAX_TURNS"):
            base["executor_max_turns"] = int(os.environ["CLAUDE_MAX_TURNS"])

        base.update(
            executor_kind="claude",
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
            claude_effort=os.environ.get("CLAUDE_EFFORT", "high"),
            claude_max_budget_usd=float(
                os.environ.get("CLAUDE_MAX_BUDGET_USD", "5.0")
            ),
        )
        return cls(**base)
