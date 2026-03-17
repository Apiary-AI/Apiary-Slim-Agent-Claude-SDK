---
name: github-pr
description: Create pull requests on GitHub repositories
---

# GitHub PR Module

You have helper scripts for a full GitHub PR workflow. Use them when asked to make changes to a GitHub repository and open a pull request.

## Available Scripts

### `clone-and-branch.sh`

Clones a GitHub repo and creates a feature branch.

```bash
clone-and-branch.sh <repo> <branch-name> [base-branch]
```

- `repo` — GitHub repo in `owner/repo` format (e.g. `acme/backend`)
- `branch-name` — name for the new feature branch
- `base-branch` — base branch to branch from (default: `main`)

The repo is cloned into `/workspace/repos/<repo-name>/`. You can then `cd` into it and make changes.

### `push-and-pr.sh`

Commits all changes, pushes the branch, and opens a pull request.

```bash
push-and-pr.sh <repo-dir> <pr-title> [pr-body]
```

- `repo-dir` — path to the cloned repo (e.g. `/workspace/repos/backend`)
- `pr-title` — title for the pull request
- `pr-body` — optional body/description (default: empty)

## Workflow

1. Use `clone-and-branch.sh` to clone the repo and create a branch
2. `cd` into the repo directory and make the requested changes
3. Use `push-and-pr.sh` to commit, push, and open the PR

## Requirements

- `GITHUB_TOKEN` must be set (configured via entrypoint.sh)
- `gh` CLI must be authenticated (done automatically if GITHUB_TOKEN is set)
