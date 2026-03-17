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

# Configure GitHub CLI auth if token provided
if [ -n "$GITHUB_TOKEN" ]; then
    echo "$GITHUB_TOKEN" | gh auth login --with-token 2>/dev/null || true
    git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
fi

# Run module setup (install deps, update CLAUDE.md)
python3 -m src.module_setup || echo "Warning: module setup failed"

exec "$@"
