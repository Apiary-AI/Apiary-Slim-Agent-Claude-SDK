"""Claude-specific config: extends BaseConfig with Anthropic API key + model knobs."""

from __future__ import annotations

import os
from dataclasses import dataclass

from superpos_agent_core import BaseConfig


@dataclass
class ClaudeConfig(BaseConfig):
    """Adds Claude-specific knobs on top of the universal BaseConfig."""

    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-8"
    claude_effort: str = "high"
    # Max seconds the claude SDK iterator may go without yielding a message
    # before we treat the subprocess as deadlocked and cancel.  Set generously
    # to survive long Bash tool calls — the per-tool SDK timeout will fire
    # first under normal conditions and advance the iterator.
    claude_stall_timeout: int = 900

    def __post_init__(self) -> None:
        if not self.executor_kind or self.executor_kind == "generic":
            self.executor_kind = "claude"
        super().__post_init__()

    @classmethod
    def from_env(cls) -> "ClaudeConfig":
        base = cls._base_env_kwargs()

        # Honour the legacy CLAUDE_* env vars in addition to the generic ones,
        # so existing .env files keep working after the port.  CLAUDE_WORKING_DIR
        # and CLAUDE_WORKTREE_ISOLATION are read independently — pre-port the
        # old config always honoured the isolation flag even when working dir
        # was left at the /workspace default, and we must preserve that.
        working_dir = os.environ.get("CLAUDE_WORKING_DIR")
        if working_dir:
            base["executor_working_dir"] = working_dir
        isolation_env = os.environ.get("CLAUDE_WORKTREE_ISOLATION")
        if isolation_env is not None:
            base["executor_worktree_isolation"] = (
                isolation_env.lower() not in ("0", "false", "no")
            )
        elif working_dir:
            # Explicit CLAUDE_WORKING_DIR with no isolation flag — re-derive
            # auto-detection from the new working dir (the base loader saw the
            # old default).
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
            claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-8"),
            claude_effort=os.environ.get("CLAUDE_EFFORT", "high"),
            claude_stall_timeout=int(os.environ.get("CLAUDE_STALL_TIMEOUT", "900")),
        )
        return cls(**base)
