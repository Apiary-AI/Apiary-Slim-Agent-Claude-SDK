"""Configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # Apiary
    apiary_base_url: str = ""
    apiary_hive_id: str = ""
    apiary_agent_id: str = ""
    apiary_api_token: str = ""
    apiary_refresh_token: str = ""
    apiary_capabilities: list[str] = field(default_factory=list)
    apiary_poll_interval: int = 5

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_users: list[int] = field(default_factory=list)
    telegram_chat_id: str = ""

    # OpenAI (for Whisper voice transcription)
    openai_api_key: str = ""

    # Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_max_budget_usd: float = 5.0
    claude_max_turns: int = 30
    claude_working_dir: str = "/workspace"
    claude_worktree_isolation: bool = False
    claude_max_parallel: int = 3

    @classmethod
    def from_env(cls) -> Config:
        allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
        caps = os.environ.get("APIARY_CAPABILITIES", "")
        working_dir = os.environ.get("CLAUDE_WORKING_DIR", "/workspace")

        isolation_env = os.environ.get("CLAUDE_WORKTREE_ISOLATION")
        if isolation_env is not None:
            worktree_isolation = isolation_env.lower() not in ("0", "false", "no")
        else:
            # Auto-enable when the working directory is a git repo
            worktree_isolation = os.path.isdir(os.path.join(working_dir, ".git"))

        return cls(
            apiary_base_url=os.environ.get("APIARY_BASE_URL", ""),
            apiary_hive_id=os.environ.get("APIARY_HIVE_ID", ""),
            apiary_agent_id=os.environ.get("APIARY_AGENT_ID", ""),
            apiary_api_token=os.environ.get("APIARY_API_TOKEN", ""),
            apiary_refresh_token=os.environ.get("APIARY_REFRESH_TOKEN", ""),
            apiary_capabilities=[c.strip() for c in caps.split(",") if c.strip()],
            apiary_poll_interval=int(os.environ.get("APIARY_POLL_INTERVAL", "5")),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_allowed_users=[
                int(u.strip()) for u in allowed.split(",") if u.strip()
            ],
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            claude_model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            claude_max_budget_usd=float(
                os.environ.get("CLAUDE_MAX_BUDGET_USD", "5.0")
            ),
            claude_max_turns=int(os.environ.get("CLAUDE_MAX_TURNS", "30")),
            claude_working_dir=working_dir,
            claude_worktree_isolation=worktree_isolation,
            claude_max_parallel=int(os.environ.get("CLAUDE_MAX_PARALLEL", "3")),
        )

    @property
    def apiary_enabled(self) -> bool:
        return bool(
            self.apiary_base_url
            and self.apiary_hive_id
            and self.apiary_agent_id
            and self.apiary_api_token
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token)
