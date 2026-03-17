#!/bin/bash
set -euo pipefail

REPO="${1:?Usage: clone-and-branch.sh <owner/repo> <branch-name> [base-branch]}"
BRANCH="${2:?Usage: clone-and-branch.sh <owner/repo> <branch-name> [base-branch]}"
BASE="${3:-main}"

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
