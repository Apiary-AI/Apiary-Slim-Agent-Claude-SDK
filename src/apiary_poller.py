"""Apiary polling daemon — polls for tasks and enqueues them for Claude."""

from __future__ import annotations

import asyncio
import json
import logging

from .apiary_client import ApiaryClient
from .claude_executor import ClaudeExecutor, ExecutionRequest
from .config import Config

log = logging.getLogger(__name__)


async def run_apiary_poller(
    apiary: ApiaryClient,
    executor: ClaudeExecutor,
    config: Config,
) -> None:
    """Poll Apiary for tasks and enqueue them for execution.

    Heartbeat is sent on every poll iteration so the agent stays online
    in the Apiary dashboard without a separate background loop.
    """
    log.info("Apiary poller started (interval=%ds)", config.apiary_poll_interval)

    try:
        while True:
            # Heartbeat first — keeps agent online in Apiary
            try:
                await apiary.heartbeat()
            except Exception:
                log.exception("Heartbeat failed")

            # Then poll for tasks
            try:
                tasks = await apiary.poll_tasks()
                for task in tasks:
                    task_id = str(task.get("id", ""))
                    # Prompt can be in payload.prompt, payload.input,
                    # invoke.instructions, or top-level fields
                    payload = task.get("payload", {}) or {}
                    invoke = task.get("invoke", {}) or {}
                    prompt = ""

                    # Priority: invoke.instructions > payload.prompt > payload.input
                    if isinstance(invoke, dict):
                        prompt = invoke.get("instructions", "")
                    if not prompt and isinstance(payload, dict):
                        prompt = payload.get("prompt", payload.get("input", ""))
                    if not prompt:
                        prompt = task.get("input", task.get("prompt", task.get("description", "")))

                    # Attach webhook/event payload as context so Claude
                    # can see the data it needs to act on.
                    context_data = task.get("payload") or task.get("event_payload")
                    if not context_data:
                        context_data = (
                            payload.get("event_payload")
                            if isinstance(payload, dict)
                            else None
                        )
                    if context_data and prompt:
                        context_json = json.dumps(
                            context_data, indent=2, ensure_ascii=False, default=str,
                        )
                        prompt = (
                            f"{prompt}\n\n---\n\n"
                            f"**Task payload data:**\n```json\n{context_json}\n```"
                        )

                    if not task_id or not prompt:
                        log.warning("Skipping task with missing id/prompt: %s", task)
                        continue

                    try:
                        await apiary.claim_task(task_id)
                    except Exception:
                        log.warning("Failed to claim task %s (maybe already claimed)", task_id)
                        continue

                    chat_id = config.telegram_chat_id
                    if not chat_id:
                        log.warning("No TELEGRAM_CHAT_ID set, skipping Apiary task notification")
                        chat_id = "0"

                    req = ExecutionRequest(
                        prompt=prompt,
                        chat_id=chat_id,
                        source="apiary",
                        apiary_task_id=task_id,
                    )
                    await executor.queue.put(req)
                    log.info("Enqueued apiary task %s (queue=%d)", task_id, executor.pending)

            except Exception:
                log.exception("Apiary poll error")

            await asyncio.sleep(config.apiary_poll_interval)

    except asyncio.CancelledError:
        log.info("Apiary poller shutting down")
        raise
