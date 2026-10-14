from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from forge.isolation.worktree_manager import (
    WorktreeError,
    create_worktree,
    remove_worktree,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """
    A real, throwaway git repository with one commit. Config is set on
    this repo only (never --global), so the test never touches whatever
    git identity is configured on the machine running it.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init")
    _git(repo_root, "config", "user.email", "forge-test@example.com")
    _git(repo_root, "config", "user.name", "Forge Test")
    _git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    _git(repo_root, "add", "README.md")
    _git(repo_root, "commit", "-m", "initial commit")
    return repo_root


def _head_commit(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD").stdout.strip()


def test_create_worktree_checks_out_expected_branch_and_commit(repo: Path) -> None:
    base_commit = _head_commit(repo)

    handle = create_worktree(repo, task_id="task-1", attempt_id="att-1", base_commit=base_commit)

    assert handle.base_commit == base_commit
    assert handle.branch_name == "forge/task-1/att-1"
    worktree_path = Path(handle.worktree_path)
    assert worktree_path == repo / ".forge" / "worktrees" / "att-1"
    assert worktree_path.is_dir()

    head_in_worktree = _git(worktree_path, "rev-parse", "HEAD").stdout.strip()
    assert head_in_worktree == base_commit

    branch_list = _git(repo, "branch", "--list", "forge/task-1/att-1").stdout
    assert "forge/task-1/att-1" in branch_list


def test_create_worktree_creates_leaf_directory_tree(repo: Path) -> None:
    # .forge/worktrees/ does not exist before the first worktree is made.
    assert not (repo / ".forge").exists()

    base_commit = _head_commit(repo)
    handle = create_worktree(repo, task_id="task-0", attempt_id="att-0", base_commit=base_commit)

    assert Path(handle.worktree_path).is_dir()


def test_create_worktree_with_bogus_base_commit_raises(repo: Path) -> None:
    bogus_commit = "deadbeef" * 5  # not a valid object id in this repo

    with pytest.raises(WorktreeError) as excinfo:
        create_worktree(repo, task_id="task-x", attempt_id="att-x", base_commit=bogus_commit)

    message = str(excinfo.value)
    # The actual git failure text must be surfaced, not swallowed or
    # replaced with a generic message.
    assert bogus_commit in message or "invalid" in message.lower()
    assert "task-x" in message
    assert "att-x" in message


def test_create_worktree_rejects_path_that_already_exists(repo: Path) -> None:
    base_commit = _head_commit(repo)
    create_worktree(repo, task_id="task-dup", attempt_id="att-dup", base_commit=base_commit)

    with pytest.raises(WorktreeError) as excinfo:
        create_worktree(repo, task_id="task-dup", attempt_id="att-dup", base_commit=base_commit)
    assert "att-dup" in str(excinfo.value)


def test_create_worktree_rejects_duplicate_branch_name(repo: Path) -> None:
    base_commit = _head_commit(repo)
    # Same task_id/attempt_id pair would reuse the same branch name even
    # at a different path; confirm git's own branch-collision error surfaces.
    create_worktree(repo, task_id="task-b", attempt_id="att-b", base_commit=base_commit)

    with pytest.raises(WorktreeError) as excinfo:
        create_worktree(repo, task_id="task-b", attempt_id="att-b", base_commit=base_commit)
    assert "forge/task-b/att-b" in str(excinfo.value) or "already exists" in str(excinfo.value)


def test_create_worktree_requires_a_git_repository(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    with pytest.raises(WorktreeError):
        create_worktree(not_a_repo, task_id="task-y", attempt_id="att-y", base_commit="HEAD")


def test_remove_worktree_removes_directory_and_branch(repo: Path) -> None:
    base_commit = _head_commit(repo)
    handle = create_worktree(repo, task_id="task-2", attempt_id="att-2", base_commit=base_commit)
    worktree_path = Path(handle.worktree_path)
    assert worktree_path.is_dir()

    remove_worktree(repo, handle)

    assert not worktree_path.exists()
    branch_list = _git(repo, "branch", "--list", handle.branch_name).stdout
    assert handle.branch_name not in branch_list


def test_remove_worktree_with_uncommitted_changes_requires_force(repo: Path) -> None:
    base_commit = _head_commit(repo)
    handle = create_worktree(repo, task_id="task-3", attempt_id="att-3", base_commit=base_commit)
    (Path(handle.worktree_path) / "README.md").write_text("changed by the attempt\n")

    with pytest.raises(WorktreeError):
        remove_worktree(repo, handle, force=False)

    # The failed attempt must not have removed anything.
    assert Path(handle.worktree_path).exists()
    branch_list = _git(repo, "branch", "--list", handle.branch_name).stdout
    assert handle.branch_name in branch_list

    remove_worktree(repo, handle, force=True)
    assert not Path(handle.worktree_path).exists()


def test_remove_worktree_is_safe_to_call_twice(repo: Path) -> None:
    base_commit = _head_commit(repo)
    handle = create_worktree(repo, task_id="task-4", attempt_id="att-4", base_commit=base_commit)

    remove_worktree(repo, handle)
    # Second call: both the worktree and the branch are already gone.
    # Must not raise.
    remove_worktree(repo, handle)


def test_remove_worktree_after_out_of_band_deletion(repo: Path) -> None:
    base_commit = _head_commit(repo)
    handle = create_worktree(repo, task_id="task-5", attempt_id="att-5", base_commit=base_commit)
    worktree_path = Path(handle.worktree_path)

    # Simulate someone deleting the worktree directory manually, bypassing
    # git entirely, leaving git's own administrative state stale.
    shutil.rmtree(worktree_path)
    assert not worktree_path.exists()

    # Must clean up (including the branch) without raising.
    remove_worktree(repo, handle)

    branch_list = _git(repo, "branch", "--list", handle.branch_name).stdout
    assert handle.branch_name not in branch_list
