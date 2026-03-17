"""CLI helper for creating Apiary tasks and schedules from within the container."""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def _base_config() -> tuple[str, str, str, str]:
    base_url = os.environ.get("APIARY_BASE_URL", "").rstrip("/")
    hive_id = os.environ.get("APIARY_HIVE_ID", "")
    agent_id = os.environ.get("APIARY_AGENT_ID", "")
    token = os.environ.get("APIARY_API_TOKEN", "")

    if not base_url or not hive_id or not token:
        print("Error: APIARY_BASE_URL, APIARY_HIVE_ID, and APIARY_API_TOKEN must be set", file=sys.stderr)
        sys.exit(1)

    return base_url, hive_id, agent_id, token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def create_task(
    prompt: str,
    task_type: str = "default",
    capability: str | None = None,
    priority: int = 2,
    self_target: bool = True,
    payload_extra: dict | None = None,
    timeout_seconds: int = 1800,
) -> None:
    """Create a task in Apiary.

    Fields reference (from Apiary CreateTaskRequest validation):
      - type (required, string, max 100)
      - payload (optional, object) — free-form task data
      - invoke.instructions (optional) — control-plane instructions for the agent
      - target_agent_id (optional, ULID) — route to a specific agent
      - target_capability (optional, string) — route to agent with capability
      - priority (optional, 0-4, default 2) — 0=lowest, 4=highest
      - timeout_seconds (optional, 1-86400, default 1800)
      - max_retries (optional, 0-10, default 3)
    """
    base_url, hive_id, agent_id, token = _base_config()

    body: dict = {
        "type": task_type,
        "payload": {"prompt": prompt, **(payload_extra or {})},
        "priority": priority,
        "timeout_seconds": timeout_seconds,
    }

    if self_target and agent_id:
        body["target_agent_id"] = agent_id
    if capability:
        body["target_capability"] = capability

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.post(
            f"/api/v1/hives/{hive_id}/tasks",
            json=body,
            headers=_headers(token),
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            task_data = data.get("data", data)
            task_id = task_data.get("id", "unknown") if isinstance(task_data, dict) else "unknown"
            print(f"Task created: {task_id}")
        else:
            print(f"Error creating task: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def create_schedule(
    name: str,
    trigger_type: str,
    task_type: str = "default",
    prompt: str | None = None,
    cron_expression: str | None = None,
    interval_seconds: int | None = None,
    run_at: str | None = None,
    self_target: bool = True,
    overlap_policy: str = "skip",
) -> None:
    """Create a task schedule in Apiary.

    Trigger types: once, interval, cron
    Overlap policies: skip, allow, cancel_previous

    Fields reference (from Apiary CreateTaskScheduleRequest):
      - name (required, string, max 150)
      - trigger_type (required: once|interval|cron)
      - cron_expression (required if cron, e.g. "*/5 * * * *")
      - interval_seconds (required if interval, min 10, max 2592000)
      - run_at (required if once, ISO8601 datetime, must be in future)
      - task_type (required, string)
      - task_payload (optional, object)
      - task_target_agent_id (optional, ULID)
      - task_target_capability (optional, string)
      - task_priority (optional, 0-4)
      - overlap_policy (optional: skip|allow|cancel_previous)
    """
    base_url, hive_id, agent_id, token = _base_config()

    body: dict = {
        "name": name,
        "trigger_type": trigger_type,
        "task_type": task_type,
        "overlap_policy": overlap_policy,
    }

    if prompt:
        body["task_payload"] = {"prompt": prompt}
    if cron_expression:
        body["cron_expression"] = cron_expression
    if interval_seconds is not None:
        body["interval_seconds"] = interval_seconds
    if run_at:
        body["run_at"] = run_at
    if self_target and agent_id:
        body["task_target_agent_id"] = agent_id

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.post(
            f"/api/v1/hives/{hive_id}/schedules",
            json=body,
            headers=_headers(token),
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            sched_data = data.get("data", data)
            sched_id = sched_data.get("id", "unknown") if isinstance(sched_data, dict) else "unknown"
            print(f"Schedule created: {sched_id}")
        else:
            print(f"Error creating schedule: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def list_schedules() -> None:
    """List all schedules in the hive."""
    base_url, hive_id, _, token = _base_config()

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            f"/api/v1/hives/{hive_id}/schedules",
            headers=_headers(token),
        )
        if resp.status_code == 200:
            data = resp.json()
            schedules = data.get("data", [])
            if not schedules:
                print("No schedules found.")
                return
            for s in schedules:
                status = s.get("status", "?")
                trigger = s.get("trigger_type", "?")
                name = s.get("name", "unnamed")
                sid = s.get("id", "?")
                next_run = s.get("next_run_at", "n/a")
                print(f"  [{status}] {sid}  {name}  ({trigger}, next: {next_run})")
        else:
            print(f"Error listing schedules: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def delete_schedule(schedule_id: str) -> None:
    """Delete a schedule."""
    base_url, hive_id, _, token = _base_config()

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.delete(
            f"/api/v1/hives/{hive_id}/schedules/{schedule_id}",
            headers=_headers(token),
        )
        if resp.status_code in (200, 204):
            print(f"Schedule {schedule_id} deleted.")
        else:
            print(f"Error deleting schedule: {resp.status_code} {resp.text}", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apiary task & schedule helper")
    sub = parser.add_subparsers(dest="command")

    # --- task create ---
    create = sub.add_parser("create", help="Create a new task")
    create.add_argument("--prompt", required=True, help="Task prompt / instructions")
    create.add_argument("--type", default="default", help="Task type (default: 'default')")
    create.add_argument("--capability", help="Required agent capability")
    create.add_argument("--priority", type=int, default=2, help="Priority 0-4 (default: 2)")
    create.add_argument("--timeout", type=int, default=1800, help="Claim timeout in seconds (default: 1800)")
    create.add_argument("--no-self-target", action="store_true", help="Don't target this agent")

    # --- schedule create ---
    sched_create = sub.add_parser("schedule", help="Create a schedule")
    sched_create.add_argument("--name", required=True, help="Schedule name")
    sched_create.add_argument("--trigger", required=True, choices=["once", "interval", "cron"],
                              help="Trigger type")
    sched_create.add_argument("--prompt", help="Task prompt")
    sched_create.add_argument("--task-type", default="default", help="Task type (default: 'default')")
    sched_create.add_argument("--cron", help="Cron expression (for trigger=cron)")
    sched_create.add_argument("--interval", type=int, help="Interval in seconds (for trigger=interval)")
    sched_create.add_argument("--run-at", help="ISO8601 datetime (for trigger=once)")
    sched_create.add_argument("--overlap", default="skip", choices=["skip", "allow", "cancel_previous"])
    sched_create.add_argument("--no-self-target", action="store_true")

    # --- schedule list ---
    sub.add_parser("schedules", help="List schedules")

    # --- schedule delete ---
    sched_del = sub.add_parser("delete-schedule", help="Delete a schedule")
    sched_del.add_argument("--id", required=True, help="Schedule ID")

    args = parser.parse_args()

    if args.command == "create":
        create_task(
            prompt=args.prompt,
            task_type=args.type,
            capability=args.capability,
            priority=args.priority,
            self_target=not args.no_self_target,
            timeout_seconds=args.timeout,
        )
    elif args.command == "schedule":
        create_schedule(
            name=args.name,
            trigger_type=args.trigger,
            task_type=args.task_type,
            prompt=args.prompt,
            cron_expression=args.cron,
            interval_seconds=args.interval,
            run_at=args.run_at,
            self_target=not args.no_self_target,
            overlap_policy=args.overlap,
        )
    elif args.command == "schedules":
        list_schedules()
    elif args.command == "delete-schedule":
        delete_schedule(args.id)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
