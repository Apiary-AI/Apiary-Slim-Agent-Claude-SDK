"""Claude runtime config: registers known models for /model list."""

from __future__ import annotations

from slim_agent_core import RuntimeConfig


class ClaudeRuntimeConfig(RuntimeConfig):
    """Runtime knobs specialized for Claude (Anthropic + MiniMax-compatible)."""

    KNOWN_MODELS: tuple[str, ...] = (
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
        "MiniMax-M2.7",
    )

    EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")
