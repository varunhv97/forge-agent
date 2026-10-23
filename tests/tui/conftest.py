from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge.controller.execution_registry import ExecutionRunOutcome


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


# A deliberately tiny two-task custom DAG (implement_exec -> unit_verify_exec,
# no deploy_exec) — exactly the shape tests/cli/test_run_resume_list.py
# already uses for a fast, real-git, no-network end-to-end run.
DAG_DEFINITION = {
    "schema_version": 1,
    "name": "implement-and-verify",
    "description": "{{ objective }}",
    "inputs": [{"name": "objective", "prompt": "What should implement_exec build?"}],
    "acceptance_criteria": [
        {
            "id": "tests_pass",
            "description": "unit tests pass",
            "verification_method": "automated_test",
            "required": True,
        }
    ],
    "tasks": [
        {
            "id": "implement",
            "node_type": "implement_exec",
            "description": "{{ objective }}",
            "depends_on": [],
            "acceptance_criterion_ids": [],
        },
        {
            "id": "verify",
            "node_type": "unit_verify_exec",
            "description": "run the unit tests implement wrote",
            "depends_on": ["implement"],
            "acceptance_criterion_ids": ["tests_pass"],
        },
    ],
}


def write_dag(repo_root: Path, name: str = "implement-and-verify") -> None:
    dags_dir = repo_root / ".forge" / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    (dags_dir / f"{name}.json").write_text(json.dumps(DAG_DEFINITION))


def fake_execution_runner(
    model: str, prompt: str, profile: object, worktree_path: str
) -> ExecutionRunOutcome:
    """
    Stands in for a real `claude` CLI session, the same way
    tests/cli/test_run_resume_list.py's fake does: writes real work into
    the worktree so the mechanical auto-commit safety net has something to
    commit, without ever touching a real LLM or network.
    """
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a passing test")
