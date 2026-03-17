"""Simple persistent session store: maps chat_id → session_id."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_PATH = "/home/agent/.claude/session_store.json"


class SessionStore:
    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                log.info("Loaded %d session(s) from %s", len(self._data), self._path)
            except (json.JSONDecodeError, OSError):
                log.warning("Failed to load session store, starting fresh")
                self._data = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data))
        except OSError:
            log.warning("Failed to persist session store to %s", self._path)

    def get(self, chat_id: int | str) -> str | None:
        return self._data.get(str(chat_id))

    def set(self, chat_id: int | str, session_id: str) -> None:
        self._data[str(chat_id)] = session_id
        self._save()

    def clear(self, chat_id: int | str) -> None:
        self._data.pop(str(chat_id), None)
        self._save()
