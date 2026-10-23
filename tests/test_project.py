from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge import project
from forge.controller import db


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_find_repo_root_from_subdirectory(git_repo: Path) -> None:
    subdir = git_repo / "a" / "b"
    subdir.mkdir(parents=True)

    assert project.find_repo_root(subdir) == git_repo


def test_find_repo_root_raises_outside_a_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()

    with pytest.raises(project.NotAGitRepoError) as exc_info:
        project.find_repo_root(not_a_repo)

    assert exc_info.value.cwd == not_a_repo


def test_ensure_forge_dir_creates_db_and_worktrees(git_repo: Path) -> None:
    paths = project.ensure_forge_dir(git_repo)

    assert paths.repo_root == git_repo
    assert paths.db_path == git_repo / ".forge" / "forge.db"
    assert paths.worktrees_dir == git_repo / ".forge" / "worktrees"
    assert paths.db_path.exists()
    assert paths.worktrees_dir.is_dir()

    # the db file is actually initialized (schema applied), not just touched
    conn = db.connect(paths.db_path)
    conn.execute("SELECT 1 FROM runs")
    conn.close()


def test_ensure_forge_dir_is_idempotent(git_repo: Path) -> None:
    first = project.ensure_forge_dir(git_repo)
    (first.worktrees_dir / "marker").write_text("keep me\n")

    second = project.ensure_forge_dir(git_repo)

    assert second == first
    assert (second.worktrees_dir / "marker").exists()


def test_ensure_forge_dir_adds_gitignore_entry_when_missing(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n")

    project.ensure_forge_dir(git_repo)

    assert ".forge/" in (git_repo / ".gitignore").read_text().splitlines()


def test_ensure_forge_dir_does_not_duplicate_existing_gitignore_entry(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("node_modules/\n.forge/\n")

    project.ensure_forge_dir(git_repo)

    lines = (git_repo / ".gitignore").read_text().splitlines()
    assert lines.count(".forge/") == 1


def test_ensure_forge_dir_recognizes_forge_db_entry_as_already_covered(git_repo: Path) -> None:
    (git_repo / ".gitignore").write_text("forge.db\n")

    project.ensure_forge_dir(git_repo)

    lines = (git_repo / ".gitignore").read_text().splitlines()
    assert ".forge/" not in lines


def test_ensure_forge_dir_creates_gitignore_when_absent(git_repo: Path) -> None:
    project.ensure_forge_dir(git_repo)

    assert ".forge/" in (git_repo / ".gitignore").read_text().splitlines()


def test_discover_project_end_to_end(git_repo: Path) -> None:
    paths = project.discover_project(git_repo)

    assert paths.repo_root == git_repo
    assert paths.db_path.exists()
    assert paths.worktrees_dir.is_dir()


def test_discover_project_raises_outside_a_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()

    with pytest.raises(project.NotAGitRepoError):
        project.discover_project(not_a_repo)


def test_init_repo_creates_and_resolves_a_fresh_repo(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh-project"

    repo_root = project.init_repo(fresh)

    assert (fresh / ".git").is_dir()
    # now resolvable the ordinary way too, and consistently so
    assert project.find_repo_root(fresh) == repo_root
