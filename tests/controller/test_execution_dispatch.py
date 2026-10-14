from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.controller.execution_registry import ExecutionHandlerRegistry, ExecutionRunOutcome
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import ControllerDependencies, run_to_completion, start_run
from forge.controller.store import Store
from forge.models.attempt import AttemptStatus
from forge.models.common import NodeKind
from forge.models.request import AcceptanceCriterion
from forge.models.run import RunStatus
from forge.nodes import register_all


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _registries():
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    mechanical_registry = MechanicalRunnerRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()
    register_all(
        node_registry, permission_registry, mechanical_registry, execution_handler_registry
    )
    return node_registry, permission_registry, mechanical_registry, execution_handler_registry


def _writes_a_passing_test_without_committing(model, prompt, profile, worktree_path):
    """
    Simulates an SDK session that wrote real work but didn't commit it
    itself — exercising dispatch_execution_attempt's auto-commit safety
    net, not just the "the model remembered to commit" happy path.
    """
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a test")


def _does_nothing(model, prompt, profile, worktree_path):
    return ExecutionRunOutcome(completed=True, summary="did nothing")


def _fails_outright(model, prompt, profile, worktree_path):
    return ExecutionRunOutcome(completed=False, summary="simulated adapter failure")


def test_implement_then_unit_verify_completes_end_to_end(store: Store, repo_root: Path):
    """
    The real worktree lifecycle, start to finish, with no mocking below the
    execution-runner boundary: a real git repo, a real worktree created by
    worktree_manager, a fake runner standing in for the LLM session (the
    only thing the offline suite can't exercise live), real git commit/diff
    deriving result_commit and changed_paths, and a real `pytest -q`
    subprocess verifying the result in that exact worktree.
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )
    model_registry = ModelRegistry([])

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=model_registry,
        execution_handler_registry=execution_handler_registry,
        execution_runner=_writes_a_passing_test_without_committing,
        repo_root=repo_root,
    )

    from tests.conftest import make_graph, make_request, make_task

    criterion = AcceptanceCriterion(
        id="c1", description="unit tests pass", verification_method="automated_test"
    )
    req = make_request(acceptance_criteria=[criterion])
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
    )
    t2 = make_task(
        "t2",
        node_type="unit_verify_exec",
        node_kind=NodeKind.MECHANICAL,
        depends_on=["t1"],
        acceptance_criterion_ids=["c1"],
        permission_profile="mechanical_default",
    )
    graph = make_graph([t1, t2])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED

    attempts = {a.task_id: a for a in store.load_attempts(run.id)}
    implement_attempt = attempts["t1"]
    verify_attempt = attempts["t2"]

    assert implement_attempt.status == AttemptStatus.PASSED
    assert implement_attempt.worktree_path  # a real worktree was created
    assert implement_attempt.result_commit is not None
    assert implement_attempt.result_commit != implement_attempt.base_commit  # auto-commit fired
    assert implement_attempt.outputs.get("changed_paths") == ["test_ok.py"]

    # unit_verify_exec ran inside implement_exec's worktree, not a fresh one
    assert verify_attempt.worktree_path == implement_attempt.worktree_path
    assert verify_attempt.status == AttemptStatus.PASSED

    evidence = [e for e in store.load_evidence(run.id) if e.acceptance_criterion_id == "c1"]
    assert len(evidence) == 1
    assert evidence[0].passed is True
    assert evidence[0].commit_sha == implement_attempt.result_commit


def test_implement_exec_with_no_changes_fails(store: Store, repo_root: Path):
    """
    If a session reports completion but left nothing committed or
    uncommitted — no actual work — the attempt is FAILED, not PASSED: a
    node can't satisfy a gate by doing nothing.
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_does_nothing,
        repo_root=repo_root,
    )

    from tests.conftest import make_graph, make_request, make_task

    req = make_request()
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
    )
    graph = make_graph([t1])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.FAILED_EXHAUSTED
    attempt = store.load_attempts(run.id)[0]
    assert attempt.status == AttemptStatus.FAILED
    assert "no changes" in (attempt.failure_summary or "").lower()


def test_execution_runner_failure_is_recorded_as_failed_attempt(store: Store, repo_root: Path):
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_fails_outright,
        repo_root=repo_root,
    )

    from tests.conftest import make_graph, make_request, make_task

    req = make_request()
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
    )
    graph = make_graph([t1])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.FAILED_EXHAUSTED
    attempt = store.load_attempts(run.id)[0]
    assert attempt.status == AttemptStatus.FAILED
    assert attempt.failure_summary is not None
    assert "simulated adapter failure" in attempt.failure_summary
