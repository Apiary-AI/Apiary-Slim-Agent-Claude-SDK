#!/bin/bash
set -euo pipefail

REPO_DIR="${1:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_TITLE="${2:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_BODY="${3:-}"

cd "$REPO_DIR"

# `gh` reads GITHUB_TOKEN from the env directly. When it isn't set (GitHub App
# path), mint a short-lived installation token from the Superpos broker so
# `gh pr create` is authenticated the same way `git push` is. Best-effort: if
# no broker connection exists, GH_TOKEN stays empty and gh falls back to its
# own auth state.
if [ -z "${GITHUB_TOKEN:-}" ]; then
    GH_TOKEN="$(python3 -m superpos_agent_core.github_auth token 2>/dev/null || true)"
    export GH_TOKEN
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "master" ]; then
    echo "ERROR: refusing to push directly to $BRANCH"
    exit 1
fi

# Stage and commit all changes
git add -A
if git diff --cached --quiet; then
    echo "No changes to commit"
    exit 1
fi

git commit -m "$PR_TITLE"

# Push branch
git push -u origin "$BRANCH"

# Create PR
if [ -n "$PR_BODY" ]; then
    gh pr create --title "$PR_TITLE" --body "$PR_BODY"
else
    gh pr create --title "$PR_TITLE" --body ""
fi

echo "Pull request created successfully."
