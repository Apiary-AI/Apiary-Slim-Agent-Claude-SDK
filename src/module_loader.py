"""Discover and load module metadata from workspace/.claude/modules/."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ModuleInfo:
    name: str
    description: str
    path: str
    scripts: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    has_mcp: bool = False
    mcp_config: dict | None = None


def discover_modules(modules_dir: str = "/workspace/.claude/modules") -> list[ModuleInfo]:
    """Scan module directories, parse module.yaml, return metadata."""
    modules_path = Path(modules_dir)
    if not modules_path.is_dir():
        return []

    modules: list[ModuleInfo] = []
    for entry in sorted(modules_path.iterdir()):
        if not entry.is_dir():
            continue

        yaml_path = entry / "module.yaml"
        if not yaml_path.exists():
            continue

        with open(yaml_path) as f:
            meta = yaml.safe_load(f) or {}

        scripts: list[str] = []
        scripts_dir = entry / "scripts"
        if scripts_dir.is_dir():
            scripts = sorted(
                p.name for p in scripts_dir.iterdir() if p.is_file()
            )

        mcp_config = meta.get("mcp")
        modules.append(
            ModuleInfo(
                name=entry.name,
                description=meta.get("description", ""),
                path=str(entry),
                scripts=scripts,
                env_vars=meta.get("env", []),
                has_mcp=mcp_config is not None,
                mcp_config=mcp_config,
            )
        )

    return modules


def generate_modules_doc(modules: list[ModuleInfo]) -> str:
    """Generate markdown listing all modules and their scripts."""
    if not modules:
        return "No modules installed.\n"

    lines = ["## Installed Modules\n"]
    for mod in modules:
        lines.append(f"### {mod.name}\n")
        lines.append(f"{mod.description}\n")

        if mod.scripts:
            lines.append("**Scripts** (available on PATH):\n")
            for script in mod.scripts:
                lines.append(f"- `{script}`")
            lines.append("")

        if mod.env_vars:
            lines.append(f"**Environment** (pre-configured, do NOT echo or print these): {', '.join(f'`{v}`' for v in mod.env_vars)}\n")

        skill_path = os.path.join(mod.path, "SKILL.md")
        if os.path.exists(skill_path):
            lines.append(f"**Skill**: See `.claude/modules/{mod.name}/SKILL.md`\n")

    return "\n".join(lines)


def collect_mcp_servers(modules: list[ModuleInfo]) -> dict:
    """Merge all module MCP configs into one dict for ClaudeCodeOptions."""
    merged: dict = {}
    for mod in modules:
        if mod.has_mcp and mod.mcp_config:
            merged.update(mod.mcp_config)
    return merged
