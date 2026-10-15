"""Recover a clean main checkout without discarding work or Git operations."""

import runpy
import subprocess

import pytest

restore = runpy.run_path("infra/coding/workspace.py")["restore_assigned_branch"]


@pytest.fixture
def checkout(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "Initial")
    git("branch", "agents/test")
    return tmp_path, git


def test_clean_main_restores_assigned_branch_and_keeps_commits(checkout):
    path, git = checkout
    git("commit", "--allow-empty", "-m", "Main work is preserved")
    main = git("rev-parse", "main")
    restore(path, "agents/test", git)
    assert git("branch", "--show-current") == "agents/test"
    assert git("rev-parse", "main") == main


def test_dirty_main_is_preserved_for_inspection(checkout):
    path, git = checkout
    (path / "work.txt").write_text("unfinished experiment")
    with pytest.raises(RuntimeError, match="uncommitted"):
        restore(path, "agents/test", git)
    assert git("branch", "--show-current") == "main"
    assert (path / "work.txt").read_text() == "unfinished experiment"


def test_unfinished_git_operation_is_never_switched(checkout):
    path, git = checkout
    (path / ".git/rebase-merge").mkdir()
    with pytest.raises(RuntimeError, match="unfinished Git operation"):
        restore(path, "agents/test", git)
    assert git("branch", "--show-current") == "main"


async def test_workspace_recovery_reason_surfaces_without_command_output(monkeypatch):
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from harness.coding.clients import CodingError, Worker
    from harness.config import Settings

    monkeypatch.setattr(
        Worker,
        "command",
        AsyncMock(
            return_value=(
                '{"error":"WORKSPACE_RECOVERY_REQUIRED",'
                '"detail":"Workspace is on main with uncommitted changes; '
                'inspect before continuing"}'
            )
        ),
    )
    worker = Worker(
        Settings(_env_file=None, database_url="unused", mattermost_url="http://mm.test")
    )
    with pytest.raises(CodingError, match="uncommitted changes"):
        await worker.workspace(uuid4(), uuid4())
