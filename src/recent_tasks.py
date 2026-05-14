"""In-memory log of recent Superpos task summaries, keyed by chat_id.

Superpos tasks run in fresh Claude sessions with no shared memory of the
Telegram conversation. When the user sees a task-completion notification in
Telegram and replies with a follow-up ("what did the failure mean?"), the
Telegram session has no context for that reference.

This log captures a compact summary of each completed Superpos task so the
next Telegram message in the same chat can be primed via system_prompt_append.
In-memory only — survives across requests but not across container restarts,
which is acceptable for transient conversational context.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    description: str
    outcome: str
    detail: str


class RecentTasksLog:
    def __init__(self, max_per_chat: int = 5) -> None:
        self._max = max_per_chat
        self._data: dict[str, deque[TaskSummary]] = {}

    def record(self, chat_id: int | str, summary: TaskSummary) -> None:
        key = str(chat_id)
        if key not in self._data:
            self._data[key] = deque(maxlen=self._max)
        self._data[key].append(summary)

    def render(self, chat_id: int | str) -> str | None:
        entries = self._data.get(str(chat_id))
        if not entries:
            return None
        lines = [
            "## Recent Background Tasks",
            (
                "These Superpos tasks ran in isolated sessions — you have no "
                "session memory of them, but the user saw notifications in "
                "this chat. Use these summaries if the user references them."
            ),
            "",
        ]
        for s in entries:
            detail = s.detail.strip().replace("\n", " ")
            if len(detail) > 400:
                detail = detail[:400] + "…"
            lines.append(
                f"- [task {s.task_id}, {s.outcome}] "
                f"Description: {s.description.strip()}. "
                f"Detail: {detail}"
            )
        return "\n".join(lines)

    def clear(self, chat_id: int | str) -> None:
        self._data.pop(str(chat_id), None)
