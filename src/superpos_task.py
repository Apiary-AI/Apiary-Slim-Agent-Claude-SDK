"""Backward-compat shim for `python3 /app/src/superpos_task.py` invocations.

The real implementation moved into superpos-agent-core.  CLAUDE.md inside the
container still references this path repeatedly, so we keep the script at
the old location and delegate to the installed package.
"""

from __future__ import annotations

from superpos_agent_core import superpos_task

if __name__ == "__main__":
    superpos_task.main()
