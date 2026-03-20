"""Tests for worktree_manager: branch inference and path helpers."""

import os
import tempfile

import pytest

from src.worktree_manager import infer_branch, is_git_repo, slot_key, worktree_path


# --- worktree_path ---

def test_worktree_path_simple():
    assert worktree_path("/workspace", "main") == "/workspace/.worktrees/main"


def test_worktree_path_slash_replaced():
    assert worktree_path("/workspace", "feature/login") == "/workspace/.worktrees/feature-login"


def test_worktree_path_spaces_replaced():
    assert worktree_path("/workspace", "fix bad name") == "/workspace/.worktrees/fix-bad-name"


# --- is_git_repo ---

def test_is_git_repo_false_for_plain_dir():
    with tempfile.TemporaryDirectory() as d:
        assert not is_git_repo(d)


def test_is_git_repo_true_when_dot_git_exists():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, ".git"))
        assert is_git_repo(d)


# --- slot_key ---

def test_slot_key_with_branch():
    assert slot_key("/workspace", "feature/login") == "/workspace/.worktrees/feature-login"


def test_slot_key_without_branch():
    assert slot_key("/workspace", None) == "__main__"


# --- infer_branch: PR head ref ---

def test_infer_branch_pr_head_ref():
    task = {
        "id": "t1",
        "event_payload": {"pull_request": {"head": {"ref": "feature/my-branch"}}},
    }
    assert infer_branch(task) == "feature/my-branch"


# --- infer_branch: push ref ---

def test_infer_branch_push_ref():
    task = {
        "id": "t2",
        "event_payload": {"ref": "refs/heads/fix-auth-bug"},
    }
    assert infer_branch(task) == "fix-auth-bug"


def test_infer_branch_push_ref_without_prefix_ignored():
    task = {
        "id": "t3",
        "event_payload": {"ref": "refs/tags/v1.0"},
    }
    # Not a branch ref, should fall through
    assert infer_branch(task) is None


# --- infer_branch: explicit payload.branch ---

def test_infer_branch_payload_branch():
    task = {
        "id": "t4",
        "payload": {"branch": "hotfix/urgent"},
        "invoke": {"instructions": "do it"},
    }
    assert infer_branch(task) == "hotfix/urgent"


# --- infer_branch: explicit invoke.branch ---

def test_infer_branch_invoke_branch():
    task = {
        "id": "t5",
        "invoke": {"branch": "release/1.2"},
    }
    assert infer_branch(task) == "release/1.2"


# --- infer_branch: event_payload nested in payload ---

def test_infer_branch_event_payload_nested_in_payload():
    task = {
        "id": "t6",
        "payload": {
            "event_payload": {"pull_request": {"head": {"ref": "nested-branch"}}},
        },
    }
    assert infer_branch(task) == "nested-branch"


# --- infer_branch: PR ref nested inside event_payload.body (Apiary webhook wrapping) ---

def test_infer_branch_pr_ref_inside_body():
    task = {
        "id": "t9",
        "payload": {
            "event_payload": {
                "action": "created",
                "repository": {"full_name": "Org/Repo"},
                "body": {
                    "action": "created",
                    "number": 42,
                    "pull_request": {"head": {"ref": "feature/from-body"}},
                },
            },
        },
    }
    assert infer_branch(task) == "feature/from-body"


def test_infer_branch_push_ref_inside_body():
    task = {
        "id": "t10",
        "payload": {
            "event_payload": {
                "action": "push",
                "body": {"ref": "refs/heads/push-from-body"},
            },
        },
    }
    assert infer_branch(task) == "push-from-body"


# --- infer_branch: no branch info ---

def test_infer_branch_returns_none_when_no_context():
    task = {"id": "t7", "invoke": {"instructions": "do something"}}
    assert infer_branch(task) is None


def test_infer_branch_returns_none_for_empty_task():
    assert infer_branch({}) is None


# --- infer_branch: PR head ref takes priority over push ref ---

def test_infer_branch_pr_ref_takes_priority_over_push_ref():
    task = {
        "id": "t8",
        "event_payload": {
            "pull_request": {"head": {"ref": "pr-branch"}},
            "ref": "refs/heads/push-branch",
        },
    }
    assert infer_branch(task) == "pr-branch"
