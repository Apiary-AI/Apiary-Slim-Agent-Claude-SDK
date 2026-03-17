#!/bin/bash
set -euo pipefail

REPO_DIR="${1:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_TITLE="${2:?Usage: push-and-pr.sh <repo-dir> <pr-title> [pr-body]}"
PR_BODY="${3:-}"

cd "$REPO_DIR"

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
