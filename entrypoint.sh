#!/bin/bash
set -e

# Restore .claude.json from backup if missing
CLAUDE_JSON="$HOME/.claude.json"
BACKUP_DIR="$HOME/.claude/backups"

if [ ! -f "$CLAUDE_JSON" ] && [ -d "$BACKUP_DIR" ]; then
    LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/.claude.json.backup.* 2>/dev/null | head -1)
    if [ -n "$LATEST_BACKUP" ]; then
        cp "$LATEST_BACKUP" "$CLAUDE_JSON"
        echo "Restored $CLAUDE_JSON from $LATEST_BACKUP"
    fi
fi

# Configure git identity if provided
if [ -n "$GIT_USER_NAME" ]; then
    git config --global user.name "$GIT_USER_NAME"
fi
if [ -n "$GIT_USER_EMAIL" ]; then
    git config --global user.email "$GIT_USER_EMAIL"
fi

# Configure GitHub CLI auth if token provided.
#
# We intentionally do NOT use `git config --global url.<token-URL>.insteadOf`
# here — that pattern embeds the token into every clone's .git/config as the
# origin remote, so `git remote -v` prints the token in cleartext and any
# command that dumps remote info leaks it. Instead we let `gh` register a
# credential helper; git fetches the token on demand and never persists it
# into repo configs or remote URLs.
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
    gh auth setup-git 2>/dev/null || true
fi

# Run module setup: install deps, symlink scripts onto PATH, update CLAUDE.md.
# --bin-dir links scripts from both workspace and core-bundled modules,
# so platform tools (e.g. superpos-issues) work even when nothing is in
# /workspace/.claude/modules.
#
# If this fails (network blip on `pip install`, broken module, etc.) the
# container still starts — the Dockerfile pre-populated modules-bin with
# build-time symlinks to workspace scripts, so those stay callable from
# PATH.  Only core-bundled tools (added at runtime) are lost in that
# degraded mode.
python3 -m superpos_agent_core.module_setup \
    --modules-dir /workspace/.claude/modules \
    --agents-md /workspace/CLAUDE.md \
    --bin-dir /workspace/.claude/modules-bin \
    || echo "Warning: module setup failed (build-time workspace symlinks remain in place)"

# Sync SubAgentDefinitions from Superpos to .claude/subagents/.
# Injects persona memory and module/skill context so that spawned Claude Code
# subagents inherit the parent agent's learned knowledge and available tooling.
#
# All three directory paths are passed explicitly — the upstream
# ``superpos_agent_core.sub_agent_sync`` CLI requires ``--subagents-dir``,
# and ``--inject-modules`` is only meaningful when both ``--modules-dir`` and
# ``--skills-dir`` are supplied as scan targets.
python3 /app/src/sync_sub_agents.py \
    --subagents-dir /workspace/.claude/subagents \
    --modules-dir /workspace/.claude/modules \
    --skills-dir /workspace/.claude/skills \
    --inject-memory --inject-modules \
    || echo "Warning: sub-agent sync failed (local subagent files remain unchanged)"

exec "$@"
