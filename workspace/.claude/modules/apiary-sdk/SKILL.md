---
name: apiary-sdk
description: Use the Apiary Python SDK to call the Apiary API — tasks, knowledge store, agents, heartbeats, service workers.
---

# Apiary SDK

The `apiary_sdk` Python package is installed and ready to import. Use it when the user asks you to interact with the Apiary platform beyond what the local `apiary_task.py` helper already covers — e.g. reading/writing knowledge entries, listing tasks, working with multiple hives, or running a service worker.

The `python3 /app/src/apiary_task.py` helper covered in the main `CLAUDE.md` is still the preferred way to **create tasks or schedules for this agent**. The SDK is for everything else: richer queries, knowledge, status, and programmatic workflows.

## Environment

These are already set in the container — you can use them directly, but **never print or echo them**:

- `APIARY_BASE_URL` — API base URL
- `APIARY_HIVE_ID` — this agent's hive
- `APIARY_AGENT_ID` — this agent's ID
- `APIARY_API_TOKEN` — bearer token
- `APIARY_REFRESH_TOKEN` — refresh token

## Quick usage

```python
import os
from apiary_sdk import ApiaryClient

with ApiaryClient(os.environ["APIARY_BASE_URL"]) as client:
    client.set_token(os.environ["APIARY_API_TOKEN"])
    # ...your calls here
```

## Reference

The full SDK documentation is vendored alongside the code. Read these **directly from the filesystem** when you need details — don't guess API shapes:

- `/workspace/.claude/modules/apiary-sdk/vendor/docs/guide/python-sdk.md` — full Python SDK guide
- `/workspace/.claude/modules/apiary-sdk/vendor/docs/guide/` — per-feature guides (auth, tasks, knowledge, heartbeats, realtime, etc.)
- `/workspace/.claude/modules/apiary-sdk/vendor/sdk/python/examples/` — runnable examples (`quickstart.py`, `worker_agent.py`, `service_worker_example.py`)
- `/workspace/.claude/modules/apiary-sdk/vendor/sdk/python/src/apiary_sdk/` — the source itself (use this as ground truth if docs and code disagree)

When the user asks something SDK-related, open the relevant guide first and work from it — the guides are richer and more up-to-date than anything you could reconstruct.

## Keeping the SDK current

The vendored SDK is a git clone that can fall behind `origin/main`. Two scripts on PATH manage this:

- `apiary-sdk-check-updates` — read-only. Reports whether the vendored SDK is behind origin and lists new commits.
- `apiary-sdk-update` — pulls the latest changes and reinstalls the Python package.

**When to use them:**

- If the user asks for a feature that is **not documented** in `vendor/docs/` and **not present** in `vendor/sdk/python/src/apiary_sdk/`, run `apiary-sdk-check-updates`. If it reports drift, run `apiary-sdk-update` and retry.
- If the user **explicitly asks** to update the SDK, run `apiary-sdk-update` directly.
- Otherwise, don't run these on your own — they're not needed for normal use and shouldn't be run on every task.

## Requirements

- Python 3.10+ (already provided by the container)
- `APIARY_*` env vars from the agent config (set via `.env`)
- Network access to the Apiary instance (already configured)
