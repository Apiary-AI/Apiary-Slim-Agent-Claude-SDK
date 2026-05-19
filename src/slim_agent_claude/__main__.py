"""Slim-Agent-Claude entry point — wires ClaudeExecutor into the core orchestrator."""

from __future__ import annotations

import asyncio
import logging

from superpos_agent_core import run_agent

from .claude_executor import ClaudeExecutor
from .config import ClaudeConfig
from .runtime_config import ClaudeRuntimeConfig

log = logging.getLogger(__name__)


async def main() -> None:
    config = ClaudeConfig.from_env()
    runtime = ClaudeRuntimeConfig.load(
        default_model=config.claude_model,
        default_effort=config.claude_effort,
        home_dir=config.home_dir,
    )

    def _factory(cfg, rt, superpos, gateway, persona):
        return ClaudeExecutor(cfg, rt, superpos, gateway, persona=persona)

    await run_agent(config, runtime, executor_factory=_factory)


def cli() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    cli()
