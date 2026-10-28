from __future__ import annotations

import json
import queue
import subprocess
from pathlib import Path

import pytest

from forge.controller.execution_registry import ExecutionRunOutcome
from forge.models.common import EffortLevel


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


# A fan-out custom DAG (no network, no deploy_exec/manual_test_exec) for
# exercising the DAG view's rendering of a real branching shape end to
# end: implement feeds both unit_verify and the integration_write ->
# integration_verify chain, so unit_verify and integration_write land at
# the same rank while integration_verify lands one rank further out.
BRANCHING_DAG_DEFINITION = {
    "schema_version": 1,
    "name": "fan-out-verify",
    "description": "{{ objective }}",
    "inputs": [{"name": "objective", "prompt": "What should implement_exec build?"}],
    "acceptance_criteria": [
        {
            "id": "unit_tests_pass",
            "description": "unit tests pass",
            "verification_method": "automated_test",
            "required": True,
        },
        {
            "id": "integration_tests_pass",
            "description": "integration tests pass",
            "verification_method": "automated_test",
            "required": True,
        },
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
            "id": "unit_verify",
            "node_type": "unit_verify_exec",
            "description": "run the unit tests implement wrote",
            "depends_on": ["implement"],
            "acceptance_criterion_ids": ["unit_tests_pass"],
        },
        {
            "id": "integration_write",
            "node_type": "integration_test_write_exec",
            "description": "write integration tests",
            "depends_on": ["implement"],
            "acceptance_criterion_ids": [],
        },
        {
            "id": "integration_verify",
            "node_type": "integration_verify_exec",
            "description": "run the integration tests integration_write wrote",
            "depends_on": ["integration_write"],
            "acceptance_criterion_ids": ["integration_tests_pass"],
        },
    ],
}


def write_branching_dag(repo_root: Path, name: str = "fan-out-verify") -> None:
    dags_dir = repo_root / ".forge" / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    (dags_dir / f"{name}.json").write_text(json.dumps(BRANCHING_DAG_DEFINITION))


def fake_execution_runner(
    model: str,
    prompt: str,
    profile: object,
    worktree_path: str,
    *,
    chat_queue: queue.Queue[str] | None = None,
    effort: EffortLevel | None = None,
    live_messages: queue.Queue[object] | None = None,
) -> ExecutionRunOutcome:
    """
    Stands in for a real `claude` CLI session, the same way
    tests/cli/test_run_resume_list.py's fake does: writes real work into
    the worktree so the mechanical auto-commit safety net has something to
    commit, without ever touching a real LLM or network. Accepts (and
    ignores) `chat_queue`/`effort`/`live_messages` purely to match
    `run_sdk_node`'s real keyword-only signature — `RunWorker` always
    calls its configured execution runner with all three keywords now
    (see `forge/tui/runner.py`), chattable or not, effort-configured or
    not, live-trace-watched or not.
    """
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a passing test")
