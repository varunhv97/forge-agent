from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forge.controller.invalidation import (
    InvalidationCheckError,
    compute_stale_evidence_ids,
    evidence_is_stale,
)
from forge.models.evidence import Evidence


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _head_commit(repo_root: Path) -> str:
    return _git(repo_root, "rev-parse", "HEAD").stdout.strip()


def _write_commit(repo_root: Path, path: str, content: str, message: str) -> str:
    target = repo_root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo_root, "add", path)
    _git(repo_root, "commit", "-m", message)
    return _head_commit(repo_root)


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


def _make_evidence(
    *,
    evidence_id: str = "ev-1",
    commit_sha: str,
    touched_paths: list[str] | None = None,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        run_id="run-1",
        attempt_id="attempt-1",
        task_id="task-1",
        kind="test_result",
        passed=True,
        commit_sha=commit_sha,
        touched_paths=touched_paths or [],
        raw_artifact_id="artifact-1",
        created_at=datetime.now(UTC),
    )


def test_descendant_commit_touching_overlapping_path_is_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)
    new_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    evidence = _make_evidence(commit_sha=base_commit, touched_paths=["src/feature.py"])

    assert evidence_is_stale(evidence, new_commit, repo) is True


def test_descendant_commit_touching_only_unrelated_path_is_not_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)
    new_commit = _write_commit(repo, "styles/app.css", "body {}\n", "touch css")

    evidence = _make_evidence(commit_sha=base_commit, touched_paths=["src/feature.py"])

    assert evidence_is_stale(evidence, new_commit, repo) is False


def test_same_commit_as_both_args_is_not_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)

    evidence = _make_evidence(commit_sha=base_commit, touched_paths=["README.md"])

    assert evidence_is_stale(evidence, base_commit, repo) is False


def test_non_descendant_sibling_branch_is_not_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)
    sibling_commit = _write_commit(repo, "src/sibling.py", "v1\n", "sibling change")

    # A sibling branch off base_commit, never merged into the lineage that
    # sibling_commit is on.
    _git(repo, "checkout", "-b", "other-branch", base_commit)
    other_commit = _write_commit(repo, "src/other.py", "v1\n", "other branch change")
    _git(repo, "checkout", "-")

    evidence = _make_evidence(commit_sha=sibling_commit, touched_paths=["src/sibling.py"])

    assert evidence_is_stale(evidence, other_commit, repo) is False


def test_older_commit_passed_as_new_commit_is_not_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)
    newer_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    # evidence.commit_sha is the *newer* commit; new_commit is the older
    # one. new_commit is an ancestor of evidence.commit_sha, not a
    # descendant, so this must not be stale.
    evidence = _make_evidence(commit_sha=newer_commit, touched_paths=["src/feature.py"])

    assert evidence_is_stale(evidence, base_commit, repo) is False


def test_empty_touched_paths_is_not_stale(repo: Path) -> None:
    base_commit = _head_commit(repo)
    new_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    evidence = _make_evidence(commit_sha=base_commit, touched_paths=[])

    assert evidence_is_stale(evidence, new_commit, repo) is False


def test_empty_commit_sha_is_not_stale_and_does_not_raise(repo: Path) -> None:
    new_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    evidence = _make_evidence(commit_sha="", touched_paths=["src/feature.py"])

    assert evidence_is_stale(evidence, new_commit, repo) is False


def test_placeholder_commit_sha_that_does_not_resolve_is_not_stale(repo: Path) -> None:
    new_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    evidence = _make_evidence(commit_sha="not-a-real-commit", touched_paths=["src/feature.py"])

    assert evidence_is_stale(evidence, new_commit, repo) is False


def test_not_a_git_repo_raises_invalidation_check_error(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()

    evidence = _make_evidence(commit_sha="abc123", touched_paths=["src/feature.py"])

    with pytest.raises(InvalidationCheckError):
        evidence_is_stale(evidence, "def456", not_a_repo)


def test_compute_stale_evidence_ids_filters_mixed_list(repo: Path) -> None:
    base_commit = _head_commit(repo)
    new_commit = _write_commit(repo, "src/feature.py", "v2\n", "touch feature")

    stale_evidence = _make_evidence(
        evidence_id="ev-stale",
        commit_sha=base_commit,
        touched_paths=["src/feature.py"],
    )
    fresh_evidence = _make_evidence(
        evidence_id="ev-fresh",
        commit_sha=base_commit,
        touched_paths=["unrelated/path.py"],
    )
    placeholder_evidence = _make_evidence(
        evidence_id="ev-placeholder",
        commit_sha="",
        touched_paths=["src/feature.py"],
    )

    result = compute_stale_evidence_ids(
        [stale_evidence, fresh_evidence, placeholder_evidence], new_commit, repo
    )

    assert result == ["ev-stale"]
