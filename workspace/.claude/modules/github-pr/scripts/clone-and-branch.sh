#!/bin/bash
set -euo pipefail

REPO="${1:?Usage: clone-and-branch.sh <owner/repo> <branch-name> [base-branch]}"
BRANCH="${2:?Usage: clone-and-branch.sh <owner/repo> <branch-name> [base-branch]}"
BASE="${3:-main}"

# Ensure the git credential helper is registered before we clone. entrypoint.sh
# already runs this at boot, but re-running is idempotent (it replaces, not
# stacks, the github.com helper) and non-fatal (|| true): for a PAT-only agent
# with GITHUB_TOKEN already set, the clone still proceeds via the existing token.
python3 -m superpos_agent_core.github_auth setup >/dev/null 2>&1 || true

REPO_NAME=$(basename "$REPO")
DEST="/workspace/repos/$REPO_NAME"

if [ -d "$DEST" ]; then
    echo "Directory $DEST already exists — pulling latest"
    cd "$DEST"
    git fetch origin
    git checkout "$BASE"
    git pull origin "$BASE"
else
    echo "Cloning $REPO into $DEST..."
    git clone "https://github.com/$REPO.git" "$DEST"
    cd "$DEST"
    git checkout "$BASE"
fi

echo "Creating branch $BRANCH from $BASE..."
git checkout -b "$BRANCH"

echo "Ready. Working directory: $DEST"
echo "Branch: $BRANCH (based on $BASE)"
