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

    # When the Claude CLI is pointed at an Anthropic-compatible *shim*
    # (e.g. MiniMax via ANTHROPIC_BASE_URL), Anthropic's hosted server tools
    # (WebSearch/WebFetch) don't exist on the other end and 400. We detect the
    # shim from the base URL and swap in provider-native tooling instead.
    anthropic_base_url: str = ""
    # MiniMax's own web-search MCP (uvx minimax-coding-plan-mcp). Loaded only
    # when a key is present; billed against MiniMax Token-Plan credits, so it
    # is kept separate from the model auth token on purpose.
    minimax_api_key: str = ""
    minimax_api_host: str = "https://api.minimax.io"

    def __post_init__(self) -> None:
        if not self.executor_kind or self.executor_kind == "generic":
            self.executor_kind = "claude"
        super().__post_init__()

    @property
    def is_native_anthropic(self) -> bool:
        """True when talking to Anthropic's own API (vs a compatible shim).

        Native = no base URL override, or one that still points at Anthropic.
        Anything else (MiniMax, other gateways) is a shim where hosted server
        tools are unavailable.
        """
        url = self.anthropic_base_url.strip().lower()
        return url == "" or "anthropic.com" in url

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
            # The CLI consumes ANTHROPIC_BASE_URL itself; we read it only to
            # detect a non-Anthropic shim and adjust tooling accordingly.
            anthropic_base_url=os.environ.get("ANTHROPIC_BASE_URL", ""),
            minimax_api_key=os.environ.get("MINIMAX_API_KEY", ""),
            minimax_api_host=os.environ.get(
                "MINIMAX_API_HOST", "https://api.minimax.io"
            ),
        )
        return cls(**base)
