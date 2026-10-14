from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.controller.attempt_commit import (
    changed_paths_between,
    commit_dirty_changes,
    current_head,
)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git(repo_path, "init")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "Test")
    (repo_path / "a.txt").write_text("hello\n")
    _git(repo_path, "add", "-A")
    _git(repo_path, "commit", "-m", "initial")
    return repo_path


def test_current_head_matches_real_commit(repo: Path):
    expected = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, check=False, text=True
    ).stdout.strip()
    assert current_head(repo) == expected


def test_commit_dirty_changes_commits_and_returns_true(repo: Path):
    before = current_head(repo)
    (repo / "b.txt").write_text("new file\n")

    committed = commit_dirty_changes(repo, "auto-commit")

    assert committed is True
    after = current_head(repo)
    assert after != before


def test_commit_dirty_changes_is_a_noop_when_clean(repo: Path):
    before = current_head(repo)

    committed = commit_dirty_changes(repo, "auto-commit")

    assert committed is False
    assert current_head(repo) == before


def test_changed_paths_between_reports_touched_files(repo: Path):
    before = current_head(repo)
    (repo / "b.txt").write_text("new file\n")
    (repo / "c.txt").write_text("another\n")
    commit_dirty_changes(repo, "add two files")
    after = current_head(repo)

    changed = changed_paths_between(repo, before, after)

    assert set(changed) == {"b.txt", "c.txt"}


def test_changed_paths_between_same_commit_is_empty(repo: Path):
    head = current_head(repo)
    assert changed_paths_between(repo, head, head) == []
