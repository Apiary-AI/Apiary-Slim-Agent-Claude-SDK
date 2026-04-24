"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    """Runtime configuration for the agent.

    Superpos fields (hive_id, capabilities, permissions) are seeded from
    env vars and overwritten at startup by ``/agents/me`` when reachable.
    The dataclass is mutable so that refresh lives here rather than in a
    parallel profile object threaded through every call site.
    """

    # Superpos
    superpos_base_url: str = ""
    superpos_hive_id: str = ""
    superpos_agent_id: str = ""
    superpos_api_token: str = ""
    superpos_refresh_token: str = ""
    superpos_capabilities: list[str] = field(default_factory=list)
    superpos_permissions: list[str] = field(default_factory=list)
    superpos_poll_interval: int = 5

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_chat_id: str = ""

    # OpenAI (for Whisper voice transcription)
    openai_api_key: str = ""

    # Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-6"
    claude_max_budget_usd: float = 5.0
    claude_max_turns: int = 30
    claude_working_dir: str = "/workspace"
    claude_worktree_isolation: bool = False
    claude_effort: str = "high"
    claude_max_parallel: int = 3

    @classmethod
    def from_env(cls) -> Config:
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        caps = os.environ.get("SUPERPOS_CAPABILITIES", "")
        working_dir = os.environ.get("CLAUDE_WORKING_DIR", "/workspace")

        isolation_env = os.environ.get("CLAUDE_WORKTREE_ISOLATION")
        if isolation_env is not None:
            worktree_isolation = isolation_env.lower() not in ("0", "false", "no")
        else:
            # Auto-enable when the working directory is a git repo
            worktree_isolation = os.path.isdir(os.path.join(working_dir, ".git"))

        return cls(
            superpos_base_url=os.environ.get("SUPERPOS_BASE_URL", ""),
            superpos_hive_id=os.environ.get("SUPERPOS_HIVE_ID", ""),
            superpos_agent_id=os.environ.get("SUPERPOS_AGENT_ID", ""),
            superpos_api_token=os.environ.get("SUPERPOS_API_TOKEN", ""),
            superpos_refresh_token=os.environ.get("SUPERPOS_REFRESH_TOKEN", ""),
            superpos_capabilities=[c.strip() for c in caps.split(",") if c.strip()],
            superpos_poll_interval=int(os.environ.get("SUPERPOS_POLL_INTERVAL", "5")),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_users=[
                int(u.strip()) for u in allowed.split(",") if u.strip()
            ],
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-6"),
            claude_effort=os.environ.get("CLAUDE_EFFORT", "high"),
            claude_max_budget_usd=float(
                os.environ.get("CLAUDE_MAX_BUDGET_USD", "5.0")
            ),
            claude_max_turns=int(os.environ.get("CLAUDE_MAX_TURNS", "30")),
            claude_working_dir=working_dir,
            claude_worktree_isolation=worktree_isolation,
            claude_max_parallel=int(os.environ.get("CLAUDE_MAX_PARALLEL", "3")),
        )

    @property
    def superpos_enabled(self) -> bool:
        return bool(
            self.superpos_base_url
            and self.superpos_hive_id
            and self.superpos_agent_id
            and self.superpos_api_token
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)

    def has_permission(self, permission: str) -> bool:
        """Check whether the agent has a given permission.

        Matches exact, ``category:*`` wildcards, and the ``admin:*``
        superwildcard.  If permissions are empty (unknown — /me failed
        and env doesn't carry them), returns True so the agent tries the
        call; the server will reject if it truly lacks the right.
        """
        if not self.superpos_permissions:
            return True
        if permission in self.superpos_permissions:
            return True
        if "admin:*" in self.superpos_permissions:
            return True
        if ":" in permission:
            category = permission.split(":", 1)[0]
            if f"{category}:*" in self.superpos_permissions:
                return True
        return False
