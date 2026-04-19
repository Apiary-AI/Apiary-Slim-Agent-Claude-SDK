"""Mutable runtime overrides for Claude model and effort.

`Config` is boot-time and frozen. This holder owns the two user-tunable knobs
that can change while the agent is running (via `/model` and `/effort` Telegram
commands) and persists them to a JSON file on the /home/agent/.claude volume
so the choice survives container restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class RuntimeConfig:
    PATH = os.path.join(
        os.environ.get("HOME", "/home/agent"), ".claude", "runtime_config.json"
    )
    KNOWN_MODELS = (
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    )
    EFFORT_LEVELS = ("low", "medium", "high", "max")
    MODEL_RE = re.compile(r"^claude-[a-z0-9][a-z0-9-]*$")

    def __init__(self, model: str, effort: str) -> None:
        self.model = model
        self.effort = effort

    @classmethod
    def load(cls, config: Config) -> "RuntimeConfig":
        rc = cls(model=config.claude_model, effort=config.claude_effort)
        if os.path.exists(cls.PATH):
            try:
                data = json.loads(Path(cls.PATH).read_text())
                if isinstance(data.get("model"), str):
                    rc.model = data["model"]
                if isinstance(data.get("effort"), str):
                    rc.effort = data["effort"]
                log.info(
                    "RuntimeConfig loaded from %s (model=%s, effort=%s)",
                    cls.PATH, rc.model, rc.effort,
                )
            except (OSError, json.JSONDecodeError) as e:
                log.warning("runtime_config.json unreadable (%s) — using env defaults", e)
        return rc

    def _save(self) -> None:
        Path(self.PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(self.PATH).write_text(json.dumps({"model": self.model, "effort": self.effort}))

    def set_model(self, model: str) -> None:
        if not self.MODEL_RE.match(model):
            raise ValueError(f"Not a valid Claude model id: {model!r}")
        self.model = model
        self._save()

    def set_effort(self, effort: str) -> None:
        if effort not in self.EFFORT_LEVELS:
            raise ValueError(
                f"Effort must be one of {', '.join(self.EFFORT_LEVELS)} — got {effort!r}"
            )
        self.effort = effort
        self._save()
