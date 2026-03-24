"""Apiary polling daemon — polls for tasks and enqueues them for Claude."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from .apiary_client import ApiaryClient
from .claude_executor import ClaudeExecutor, ExecutionRequest
from .config import Config
from .worktree_manager import infer_branch

log = logging.getLogger(__name__)

# Cooldown (seconds) for deduplicating webhook events on the same entity.
# Multiple webhook events for the same repo+PR/issue within this window
# are auto-completed — only the first triggers an execution.
WEBHOOK_ENTITY_COOLDOWN = 300

# Maximum number of times a task can be re-claimed after claim expiry.
# After this limit the task is left for the server to handle (timeout/fail).
MAX_TASK_CLAIMS = 3


def _webhook_entity_key(task: dict) -> str | None:
    """Extract dedup key for a webhook task (e.g. 'owner/repo:pr:123').

    Covers multiple GitHub event types:
    - pull_request / pull_request_review / pull_request_review_comment → pr number
    - issues / issue_comment → issue number
    - push → repo:push:branch (bot's own commits trigger these)
    - check_run / check_suite → nested pull_requests array
    - fallback → repo-level dedup if repo is known
    """
    payload = task.get("payload", {}) or {}
    event_payload = payload.get("event_payload") if isinstance(payload, dict) else None
    if not isinstance(event_payload, dict):
        return None
    repo = (event_payload.get("repository") or {}).get("full_name")
    if not repo:
        return None

    # PR events (pull_request, pull_request_review, pull_request_review_comment)
    pr = event_payload.get("pull_request") or {}
    if isinstance(pr, dict) and pr.get("number"):
        return f"{repo}:pr:{pr['number']}"

    # Issue events
    issue = event_payload.get("issue") or {}
    if isinstance(issue, dict) and issue.get("number"):
        return f"{repo}:issue:{issue['number']}"

    # Push events — dedup by branch
    ref = event_payload.get("ref")
    if ref and isinstance(ref, str):
        branch = ref.removeprefix("refs/heads/")
        return f"{repo}:push:{branch}"

    # check_run / check_suite — nested pull_requests array
    for key in ("check_run", "check_suite"):
        check = event_payload.get(key)
        if isinstance(check, dict):
            prs = check.get("pull_requests") or []
            if isinstance(prs, list) and prs:
                pr_num = prs[0].get("number")
                if pr_num:
                    return f"{repo}:pr:{pr_num}"

    # No fallback — if we can't extract a specific entity, don't dedup.
    # A repo-level fallback would incorrectly dedup different PRs/issues.
    return None


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

    persona_version: int | None = None
    recent_webhook_entities: dict[str, tuple[float, str]] = {}  # entity_key -> (mono_ts, primary_task_id)
    task_claim_counts: dict[str, int] = {}  # task_id -> number of times claimed
    _failed_tasks: set[str] = set()  # tasks we already failed on the server

    try:
        while True:
            # Heartbeat first — keeps agent online in Apiary
            try:
                await apiary.heartbeat()
            except Exception:
                log.exception("Heartbeat failed")

            # Check for persona changes
            try:
                ver_data = await apiary.get_persona_version(known_version=persona_version)
                data = ver_data.get("data", ver_data) if isinstance(ver_data, dict) else {}
                changed = data.get("changed", False)
                server_version = data.get("version")
                if changed or (server_version is not None and server_version != persona_version):
                    new_persona = await apiary.get_persona_assembled()
                    executor.update_persona(new_persona)
                    persona_version = server_version
                    log.info("Persona refreshed (version=%s)", persona_version)
                elif persona_version is None and server_version is not None:
                    persona_version = server_version  # initial sync
            except Exception:
                log.debug("Persona version check failed", exc_info=True)

            # Then poll for tasks
            try:
                tasks = await apiary.poll_tasks()

                # Expire old webhook entity entries
                _now = time.monotonic()
                recent_webhook_entities = {
                    k: v for k, v in recent_webhook_entities.items()
                    if _now - v[0] < WEBHOOK_ENTITY_COOLDOWN
                }

                for task in tasks:
                    task_id = str(task.get("id", ""))

                    # Webhook dedup: auto-complete events for recently handled entities
                    if task.get("type") == "webhook_handler" and task_id:
                        entity_key = _webhook_entity_key(task)
                        if entity_key and entity_key in recent_webhook_entities:
                            _, primary_id = recent_webhook_entities[entity_key]
                            try:
                                await apiary.claim_task(task_id)
                                await apiary.complete_task(
                                    task_id,
                                    f"Consolidated: duplicate webhook for {entity_key}, "
                                    f"already handled by task {primary_id}.",
                                )
                                log.info(
                                    "Auto-completed duplicate webhook task %s (entity=%s)",
                                    task_id, entity_key,
                                )
                            except Exception:
                                log.debug("Failed to auto-complete duplicate webhook %s", task_id)
                            continue

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

                    # webhook_handler tasks may carry only event_payload and no prompt.
                    # Synthesize a prompt so the agent can act on the event.
                    if not prompt and task.get("type") == "webhook_handler":
                        event_payload = (
                            payload.get("event_payload")
                            if isinstance(payload, dict)
                            else None
                        )
                        if isinstance(event_payload, dict):
                            action = event_payload.get("action", "unknown")
                            repo = (event_payload.get("repository") or {}).get("full_name", "unknown")
                            prompt = (
                                f"Handle this GitHub webhook event: action={action}, "
                                f"repo={repo}. Inspect the attached payload for full details."
                            )

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

                    if executor.has_apiary_task(task_id):
                        log.debug("Skipping already in-flight task %s", task_id)
                        continue

                    # Stop re-claiming tasks that keep expiring — prevents
                    # infinite claim-expire-reclaim loops.  Claim + fail the
                    # task so the server removes it from the pending queue,
                    # otherwise it clogs every poll response forever.
                    prior_claims = task_claim_counts.get(task_id, 0)
                    if prior_claims >= MAX_TASK_CLAIMS:
                        if task_id not in _failed_tasks:
                            log.warning(
                                "Task %s claimed %d times — failing on server",
                                task_id, prior_claims,
                            )
                            try:
                                await apiary.claim_task(task_id)
                                await apiary.fail_task(
                                    task_id,
                                    f"Agent gave up after {prior_claims} claim attempts "
                                    f"(claims kept expiring).",
                                )
                            except Exception:
                                log.debug("Failed to fail zombie task %s", task_id)
                            _failed_tasks.add(task_id)
                        continue

                    # Don't claim new tasks while the executor is busy.
                    # Claiming eagerly causes claim timeouts for queued tasks,
                    # which sends them back to pending and creates a retry storm.
                    if not executor.has_free_slots:
                        log.debug("Executor at capacity (%d slots), deferring remaining tasks",
                                  config.claude_max_parallel)
                        break

                    try:
                        await apiary.claim_task(task_id)
                    except Exception:
                        log.warning("Failed to claim task %s (maybe already claimed)", task_id)
                        continue

                    task_claim_counts[task_id] = prior_claims + 1
                    executor.add_apiary_task(task_id)

                    chat_id = config.telegram_chat_id
                    if not chat_id:
                        log.warning("No TELEGRAM_CHAT_ID set, skipping Apiary task notification")
                        chat_id = "0"

                    branch = infer_branch(task)
                    if branch:
                        log.debug("Inferred branch %r for task %s", branch, task_id)

                    # Track webhook entity for cross-cycle dedup
                    if task.get("type") == "webhook_handler":
                        ek = _webhook_entity_key(task)
                        if ek:
                            recent_webhook_entities[ek] = (time.monotonic(), task_id)

                    req = ExecutionRequest(
                        prompt=prompt,
                        chat_id=chat_id,
                        source="apiary",
                        apiary_task_id=task_id,
                        branch=branch,
                    )
                    await executor.queue.put(req)
                    log.info("Enqueued apiary task %s (queue=%d)", task_id, executor.pending)

            except Exception:
                log.exception("Apiary poll error")

            await asyncio.sleep(config.apiary_poll_interval)

    except asyncio.CancelledError:
        log.info("Apiary poller shutting down")
        raise
