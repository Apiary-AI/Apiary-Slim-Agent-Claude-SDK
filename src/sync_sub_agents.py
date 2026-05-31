"""Sync Superpos SubAgentDefinitions → .claude/subagents/ directory.

Fetches active sub-agent definitions from the Superpos API, writes each as a
Claude Code subagent ``.md`` file, and removes stale managed files that no
longer exist on the platform.

Optionally injects the agent's persona MEMORY and a summary of installed
modules/skills so that subagents inherit the parent agent's learned context
and available tooling.

Run at container startup from entrypoint.sh, or on-demand:

    python3 /app/src/sync_sub_agents.py
    python3 /app/src/sync_sub_agents.py --inject-memory --inject-modules
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

MANAGED_MARKER = "<!-- managed-by: superpos-sync -->"
DOCUMENT_ORDER = ("SOUL", "AGENT", "RULES", "STYLE", "EXAMPLES", "NOTES")


def _base_config() -> tuple[str, str, str, str]:
    base_url = os.environ.get("SUPERPOS_BASE_URL", "").rstrip("/")
    hive_id = os.environ.get("SUPERPOS_HIVE_ID", "")
    agent_id = os.environ.get("SUPERPOS_AGENT_ID", "")
    token = os.environ.get("SUPERPOS_API_TOKEN", "")
    if not base_url or not hive_id or not token:
        print(
            "Error: SUPERPOS_BASE_URL, SUPERPOS_HIVE_ID, and SUPERPOS_API_TOKEN must be set",
            file=sys.stderr,
        )
        sys.exit(1)
    return base_url, hive_id, agent_id, token


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _get_document_content(doc_value: str | dict | None) -> str | None:
    """Extract content string from a document value (handles both string and object formats)."""
    if doc_value is None:
        return None
    if isinstance(doc_value, str):
        return doc_value if doc_value.strip() else None
    if isinstance(doc_value, dict):
        content = doc_value.get("content", "")
        return content if isinstance(content, str) and content.strip() else None
    return None


def fetch_runtime_bundle(
    base_url: str, token: str,
) -> dict | None:
    """Fetch all definitions + agent memory in one call via runtime-bundle endpoint.

    Returns dict with 'definitions', 'agent_memory', 'persona_version' or None
    if the endpoint is not available (older backend).
    """
    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get("/api/v1/sub-agents/runtime-bundle", headers=_headers(token))
        if resp.status_code != 200:
            return None
        data = resp.json()
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if not isinstance(payload, dict) or "definitions" not in payload:
            return None
        return payload


def fetch_sub_agent_definitions(
    base_url: str, token: str,
) -> list[dict]:
    """Fetch all active sub-agent definitions with full documents (N+1 fallback)."""
    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get("/api/v1/sub-agents", headers=_headers(token))
        if resp.status_code != 200:
            log.warning("Failed to list sub-agents: %s %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        summaries = data.get("data", data) if isinstance(data, dict) else []
        if not isinstance(summaries, list):
            return []

        definitions = []
        for summary in summaries:
            slug = summary.get("slug")
            if not slug:
                continue
            detail_resp = client.get(
                f"/api/v1/sub-agents/{slug}", headers=_headers(token),
            )
            if detail_resp.status_code == 200:
                detail_data = detail_resp.json()
                definition = detail_data.get("data", detail_data)
                if isinstance(definition, dict):
                    definitions.append(definition)
            else:
                log.warning("Failed to fetch sub-agent %s: %s", slug, detail_resp.status_code)

        return definitions


def fetch_persona_memory(base_url: str, token: str) -> str | None:
    """Fetch the MEMORY document from the active persona (fallback)."""
    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(
            "/api/v1/persona/documents/MEMORY", headers=_headers(token),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        payload = data.get("data", data) if isinstance(data, dict) else {}
        if isinstance(payload, dict):
            return _get_document_content(payload.get("content"))
        return None


def discover_local_context(
    modules_dir: str | None,
    skills_dir: str | None,
    subagent_slugs: list[str],
) -> str:
    """Build a context summary of available modules, skills, and sibling subagents."""
    sections = []

    if modules_dir and Path(modules_dir).is_dir():
        module_names = []
        for entry in sorted(Path(modules_dir).iterdir()):
            if entry.is_dir() and (entry / "module.yaml").exists():
                module_names.append(entry.name)
        if module_names:
            lines = ["**Installed modules** (available on PATH):"]
            for name in module_names:
                lines.append(f"- `{name}`")
            sections.append("\n".join(lines))

    if skills_dir and Path(skills_dir).is_dir():
        skill_names = []
        for entry in sorted(Path(skills_dir).iterdir()):
            if entry.suffix == ".md":
                skill_names.append(entry.stem)
        if skill_names:
            lines = ["**Available skills** (invoke with `/skill-name`):"]
            for name in skill_names:
                lines.append(f"- `/{name}`")
            sections.append("\n".join(lines))

    if subagent_slugs:
        lines = ["**Sibling subagents** (delegate with `Agent(subagent_type=\"name\")`):"]
        for slug in sorted(subagent_slugs):
            lines.append(f"- `{slug}`")
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


def assemble_prompt(documents: dict) -> str:
    """Assemble documents into a prompt, matching server-side logic."""
    parts = []
    for doc_name in DOCUMENT_ORDER:
        content = _get_document_content(documents.get(doc_name))
        if content:
            parts.append(f"# {doc_name}\n\n{content}")
    return "\n\n".join(parts)


def build_subagent_md(
    definition: dict,
    *,
    memory: str | None = None,
    local_context: str | None = None,
) -> str:
    """Build the content of a .claude/subagents/{slug}.md file."""
    slug = definition["slug"]
    name = definition.get("name", slug)
    description = definition.get("description", "")
    model = definition.get("model") or None
    config = definition.get("config") or {}
    allowed_tools = definition.get("allowed_tools")
    version = definition.get("version", 1)
    documents = definition.get("documents") or {}

    if not model and isinstance(config, dict):
        llm_config = config.get("llm", {})
        if isinstance(llm_config, dict):
            model = llm_config.get("model")

    desc_text = f"{name} — {description}" if description else name

    frontmatter_lines = [
        "---",
        f"name: {slug}",
        f"description: \"{desc_text}\"",
    ]
    if model:
        frontmatter_lines.append(f"model: {model}")
    frontmatter_lines.append(f"# synced from Superpos SubAgentDefinition v{version}")
    frontmatter_lines.append("---")

    body = assemble_prompt(documents)

    parts = ["\n".join(frontmatter_lines), ""]

    if body:
        parts.append(body)

    if allowed_tools:
        tools_str = ", ".join(f"`{t}`" for t in allowed_tools)
        parts.append(f"## Allowed Tools\n\nYou may only use these tools: {tools_str}")

    if memory and memory.strip():
        parts.append(
            "## Agent Memory\n\n"
            "The following is context the parent agent has learned. "
            "Use it to inform your work:\n\n"
            + memory.strip()
        )

    if local_context and local_context.strip():
        parts.append(f"## Agent Capabilities\n\n{local_context.strip()}")

    parts.append(MANAGED_MARKER)
    parts.append("")

    return "\n\n".join(parts)


def sync_sub_agents(
    subagents_dir: str,
    base_url: str,
    token: str,
    *,
    inject_memory: bool = False,
    modules_dir: str | None = None,
    skills_dir: str | None = None,
) -> int:
    """Sync SubAgentDefinitions from Superpos to .claude/subagents/.

    Returns the number of definitions synced.
    """
    Path(subagents_dir).mkdir(parents=True, exist_ok=True)

    # Try the single-call runtime-bundle endpoint first; fall back to N+1
    # calls for backends that haven't deployed it yet.
    bundle = fetch_runtime_bundle(base_url, token)

    if bundle is not None:
        definitions = bundle.get("definitions") or []
        memory = bundle.get("agent_memory") if inject_memory else None
        log.info(
            "Fetched runtime bundle: %d definition(s), memory=%s",
            len(definitions), "yes" if memory else "no",
        )
    else:
        definitions = fetch_sub_agent_definitions(base_url, token)
        memory = None
        if inject_memory and definitions:
            memory = fetch_persona_memory(base_url, token)

    if memory:
        log.info("Agent MEMORY: %d chars", len(memory))

    slugs = [d["slug"] for d in definitions if "slug" in d]

    local_context: str | None = None
    if modules_dir or skills_dir:
        local_context = discover_local_context(modules_dir, skills_dir, slugs)

    synced = 0
    for defn in definitions:
        slug = defn.get("slug")
        if not slug:
            continue

        content = build_subagent_md(
            defn,
            memory=memory,
            local_context=local_context,
        )

        target = Path(subagents_dir) / f"{slug}.md"
        target.write_text(content, encoding="utf-8")
        log.info("Synced sub-agent: %s (v%s)", slug, defn.get("version", "?"))
        synced += 1

    for existing in Path(subagents_dir).glob("*.md"):
        if existing.stem not in slugs:
            try:
                file_content = existing.read_text(encoding="utf-8")
            except OSError:
                continue
            if MANAGED_MARKER in file_content:
                existing.unlink()
                log.info("Removed stale managed sub-agent: %s", existing.stem)

    return synced


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Sync Superpos SubAgentDefinitions to .claude/subagents/",
    )
    parser.add_argument(
        "--subagents-dir",
        default="/workspace/.claude/subagents",
        help="Target directory for subagent .md files (default: /workspace/.claude/subagents)",
    )
    parser.add_argument(
        "--inject-memory",
        action="store_true",
        help="Inject the agent's persona MEMORY into each subagent prompt",
    )
    parser.add_argument(
        "--inject-modules",
        action="store_true",
        help="Inject a summary of available modules, skills, and sibling subagents",
    )
    parser.add_argument(
        "--modules-dir",
        default="/workspace/.claude/modules",
        help="Modules directory to scan (default: /workspace/.claude/modules)",
    )
    parser.add_argument(
        "--skills-dir",
        default="/workspace/.claude/skills",
        help="Skills directory to scan (default: /workspace/.claude/skills)",
    )

    args = parser.parse_args()
    base_url, _, _, token = _base_config()

    count = sync_sub_agents(
        subagents_dir=args.subagents_dir,
        base_url=base_url,
        token=token,
        inject_memory=args.inject_memory,
        modules_dir=args.modules_dir if args.inject_modules else None,
        skills_dir=args.skills_dir if args.inject_modules else None,
    )

    print(f"Synced {count} sub-agent definition(s)")


if __name__ == "__main__":
    main()
