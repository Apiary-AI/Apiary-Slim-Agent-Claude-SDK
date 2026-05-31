"""Thin wrapper — delegates to superpos_agent_core.sub_agent_sync.

This shim exists so that entrypoint.sh can keep calling
``python3 /app/src/sync_sub_agents.py`` without change.  All logic
now lives in the shared core package.
"""

from superpos_agent_core.sub_agent_sync import (  # noqa: F401
    MANAGED_MARKER,
    assemble_prompt,
    build_subagent_md,
    discover_local_context,
    fetch_persona_memory,
    fetch_runtime_bundle,
    fetch_sub_agent_definitions,
    sync_sub_agents,
    _get_document_content,
)


if __name__ == "__main__":
    from superpos_agent_core.sub_agent_sync import main
    main()
