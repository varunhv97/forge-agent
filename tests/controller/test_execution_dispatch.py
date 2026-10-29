from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge.controller.execution_registry import (
    EffortSwapRequest,
    ExecutionHandlerRegistry,
    ExecutionRunOutcome,
)
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import ControllerDependencies, run_to_completion, start_run
from forge.controller.store import Store
from forge.controller.validator import compile_and_validate
from forge.models.attempt import AttemptStatus
from forge.models.common import NodeKind
from forge.models.model_registry import ModelCapability
from forge.models.permissions import Budgets
from forge.models.request import AcceptanceCriterion
from forge.models.run import RunStatus
from forge.modes.custom_dag import build_custom_dag_request_and_graph
from forge.nodes import register_all


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, text=True
    )
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


def _writes_a_passing_test_without_committing(
    model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
):
    """
    Simulates an SDK session that wrote real work but didn't commit it
    itself — exercising dispatch_execution_attempt's auto-commit safety
    net, not just the "the model remembered to commit" happy path.
    """
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a test")


def _does_nothing(model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None):
    return ExecutionRunOutcome(completed=True, summary="did nothing")


def _fails_outright(model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None):
    return ExecutionRunOutcome(completed=False, summary="simulated adapter failure")


def _writes_a_passing_test_with_session_id(
    model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
):
    """Same shape as `_writes_a_passing_test_without_committing`, but with a
    real-looking Claude Agent SDK session id attached, the way `run_sdk_node`
    reports one for a real session."""
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a test", session_id="sess-live-001")


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


def test_sdk_session_id_lands_on_the_execution_attempt_but_not_a_mechanical_one(
    store: Store, repo_root: Path
):
    """
    `ExecutionRunOutcome.session_id` must be threaded onto the Attempt row
    `dispatch_execution_attempt` records — this is the capture-and-expose
    feature's core plumbing. A MECHANICAL attempt (here, unit_verify_exec)
    never goes through `dispatch_execution_attempt` at all, so it must keep
    `sdk_session_id == None` — this is not a Claude Agent SDK concept for a
    mechanical runner.
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
        execution_runner=_writes_a_passing_test_with_session_id,
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
    assert attempts["t1"].sdk_session_id == "sess-live-001"
    assert attempts["t2"].sdk_session_id is None


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


def test_mechanical_attempt_never_reaches_an_effort_aware_execution_runner(
    store: Store, repo_root: Path
):
    """
    implement_exec (EXECUTION) is registered with a real default_effort
    ("high", see forge/nodes/implement_exec.py); unit_verify_exec
    (MECHANICAL) has none at all and never goes through
    dispatch_execution_attempt in the first place. A capturing
    execution_runner proves both halves at once: it's called exactly once
    (for implement_exec, with effort=="high"), never for unit_verify_exec
    — a MECHANICAL attempt is completely unaffected by this feature, the
    same as it never had a model_used override before it existed.
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )
    calls: list[tuple[str, object]] = []

    def _capturing_runner(
        model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
    ):
        calls.append((model, effort))
        return _writes_a_passing_test_without_committing(
            model, prompt, profile, worktree_path, effort=effort
        )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_capturing_runner,
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
    assert calls == [("claude-sonnet-5", "high")]

    attempts = {a.task_id: a for a in store.load_attempts(run.id)}
    assert attempts["t2"].model_used == "mechanical"


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


# -- task-level model/effort overrides (phase 2 of the model/effort --------
# -- customization arc) -----------------------------------------------------

_OVERRIDE_DAG_DEFINITION = {
    "schema_version": 1,
    "name": "override-test",
    "description": "a single implement_exec task with model/effort overrides",
    "inputs": [],
    "acceptance_criteria": [],
    "tasks": [
        {
            "id": "implement",
            "node_type": "implement_exec",
            "description": "do the thing",
            "depends_on": [],
            "acceptance_criterion_ids": [],
            "model": "claude-opus-5",
            "effort": "low",
        }
    ],
}


def test_custom_dag_task_level_model_and_effort_overrides_reach_the_execution_runner(
    store: Store, repo_root: Path, tmp_path: Path
):
    """
    Not just a Pydantic round-trip: a real custom-DAG JSON file, loaded
    through build_custom_dag_request_and_graph, compiled, and run through
    the real scheduler with a capturing execution_runner — proving the
    task's `model`/`effort` overrides actually reach the runner on a fresh
    dispatch, in place of implement_exec's own registered default_model
    ("claude-sonnet-5") and default_effort ("high").
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )
    model_registry = ModelRegistry(
        [
            ModelCapability(
                provider="anthropic",
                model_id="claude-opus-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=200000,
                cost_per_mtok_in=15.0,
                cost_per_mtok_out=75.0,
                eligible_roles=["implement_exec"],
            )
        ]
    )

    dag_path = tmp_path / "override-test.json"
    dag_path.write_text(json.dumps(_OVERRIDE_DAG_DEFINITION))

    request, draft = build_custom_dag_request_and_graph(
        dag_path,
        {},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )
    graph = compile_and_validate(draft, request, node_registry, permission_registry)

    calls: list[tuple[str, object]] = []

    def _capturing_runner(
        model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
    ):
        calls.append((model, effort))
        return _writes_a_passing_test_without_committing(
            model, prompt, profile, worktree_path, effort=effort
        )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=model_registry,
        execution_handler_registry=execution_handler_registry,
        execution_runner=_capturing_runner,
        repo_root=repo_root,
    )

    run = start_run(request, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert calls == [("claude-opus-5", "low")]

    attempt = store.load_attempts(run.id)[0]
    assert attempt.model_used == "claude-opus-5"


def test_effort_override_replaces_only_the_default_rung_escalation_still_applies(
    store: Store, repo_root: Path
):
    """
    The design decision this phase made explicit (see architecture.md):
    a task-level effort_override replaces only node_type.default_effort's
    rung in select_effort's policy — node_type.escalation_effort still
    applies after repeated failures, rather than the override disabling
    escalation outright. implement_exec is registered with
    default_effort="high"/escalation_effort="max"; this task overrides
    only the starting point to "low". The first two attempts (0 and 1
    prior FAILED attempts, below ESCALATION_FAILURE_THRESHOLD) must both
    run at the override's "low", not implement_exec's own "high"; the
    third (2 prior FAILED attempts) must escalate to "max" — the node
    type's own escalation_effort, never "low" again and never a value the
    override invented.
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )
    efforts_seen: list[object] = []

    def _fails_twice_then_passes(
        model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
    ):
        efforts_seen.append(effort)
        if len(efforts_seen) < 3:
            return ExecutionRunOutcome(completed=False, summary="simulated failure")
        Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
        return ExecutionRunOutcome(completed=True, summary="wrote a test")

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_fails_twice_then_passes,
        repo_root=repo_root,
    )

    from tests.conftest import make_graph, make_request, make_task

    req = make_request()
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
        effort_override="low",
    )
    graph = make_graph([t1])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert efforts_seen == ["low", "low", "max"]


# -- interrupt-and-resume model/effort swap (post-M9 phase 4) ---------------


def test_effort_swap_interrupted_attempt_is_recorded_interrupted_for_swap_and_resumed(
    store: Store, repo_root: Path
):
    """
    End to end through the real scheduler loop (`run_to_completion`, not a
    single `dispatch_execution_attempt` call): an `execution_runner` that
    reports `swap_requested` on its first call must not be scored as a
    normal pass/fail attempt at all — the attempt is recorded
    `INTERRUPTED_FOR_SWAP`, with no evidence and no `failure_summary` (it
    isn't a failure), carrying the session id to resume from and the
    requested model/effort forward (`pending_resume_model`/
    `pending_resume_effort`). The *next* dispatch for the same task — which
    only happens because an INTERRUPTED_FOR_SWAP latest attempt leaves the
    task ready again, the same way CANCELLED/SUPERSEDED already do — must
    receive exactly that `resume_session_id`/model/effort, bypassing the
    normal override-or-default model expression and `select_effort`
    entirely (the runner's own second call records `effort="max"` with no
    escalation history at all behind it — two prior FAILED attempts would
    have been needed to reach "max" through the ordinary policy, and there
    are zero here).
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )
    calls: list[dict[str, object]] = []

    def _interrupts_once_then_passes(
        model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
    ):
        calls.append({"model": model, "effort": effort, "resume_session_id": resume_session_id})
        if len(calls) == 1:
            return ExecutionRunOutcome(
                completed=True,
                summary="interrupted for a swap",
                session_id="sess-to-resume-xyz",
                swap_requested=EffortSwapRequest(effort="max", model="claude-opus-5"),
            )
        Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
        return ExecutionRunOutcome(completed=True, summary="wrote a test")

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_interrupts_once_then_passes,
        repo_root=repo_root,
    )

    from tests.conftest import make_graph, make_request, make_task

    criterion = AcceptanceCriterion(
        id="c1", description="implement_exec's own work", verification_method="automated_test"
    )
    req = make_request(acceptance_criteria=[criterion])
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
        acceptance_criterion_ids=["c1"],
    )
    graph = make_graph([t1])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert len(calls) == 2

    # First dispatch: nothing to resume yet, ordinary default model/effort.
    assert calls[0]["resume_session_id"] is None

    # Second dispatch: the swap's own requested model/effort, and the
    # exact session to resume from — never select_model's/select_effort's
    # own escalation policy (which would need 2 prior FAILED attempts to
    # ever produce "max", and there are none here).
    assert calls[1]["resume_session_id"] == "sess-to-resume-xyz"
    assert calls[1]["model"] == "claude-opus-5"
    assert calls[1]["effort"] == "max"

    attempts = sorted(store.load_attempts(run.id), key=lambda a: a.attempt_number)
    assert len(attempts) == 2
    interrupted, resumed = attempts

    assert interrupted.status == AttemptStatus.INTERRUPTED_FOR_SWAP
    assert interrupted.failure_summary is None
    assert interrupted.sdk_session_id == "sess-to-resume-xyz"
    assert interrupted.pending_resume_model == "claude-opus-5"
    assert interrupted.pending_resume_effort == "max"
    # Not scored as a pass/fail attempt: no evidence produced for it at all.
    assert not [e for e in store.load_evidence(run.id) if e.attempt_id == interrupted.id]

    assert resumed.status == AttemptStatus.PASSED
    assert resumed.model_used == "claude-opus-5"
    # The interrupted attempt is never miscounted as a failure by the
    # escalation policy — calls[1]["effort"] == "max" above came from the
    # swap's own override, not from select_effort's 2-prior-FAILED-
    # attempts escalation rule (there are zero FAILED attempts here at
    # all); see tests/controller/test_model_registry.py::TestSelectEffort
    # ::test_interrupted_for_swap_attempts_are_never_counted_as_failures
    # for the same claim proven directly against select_effort/
    # _prior_failures.


def test_interrupted_for_swap_attempt_does_not_trip_unrecoverable_failure_detection(
    store: Store, repo_root: Path
):
    """
    `_has_unrecoverable_failure` (the check that sends a run straight to
    FAILED_EXHAUSTED) only ever looks at a required task's *latest*
    attempt status — if that status is INTERRUPTED_FOR_SWAP rather than
    FAILED, the run must stay ACTIVE and keep making progress, not be
    declared failed. Uses an execution_runner that only ever reports a
    swap interruption (never a real pass/fail), driven for a bounded
    number of ticks directly rather than through run_to_completion (which
    would loop forever against a runner that never actually finishes the
    task) — this isolates exactly the one check this test is about.
    """
    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _registries()
    )

    def _always_interrupts(
        model, prompt, profile, worktree_path, *, effort=None, resume_session_id=None
    ):
        return ExecutionRunOutcome(
            completed=True,
            summary="interrupted for a swap",
            session_id="sess-again",
            swap_requested=EffortSwapRequest(effort="max"),
        )

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=_always_interrupts,
        repo_root=repo_root,
    )

    from forge.controller.scheduler import controller_tick
    from tests.conftest import make_graph, make_request, make_task

    # A required acceptance criterion tied to t1 so the run's gates are
    # never vacuously "met" regardless of t1's status — without this, a
    # request with no acceptance criteria at all would make the run
    # COMPLETED_VERIFIED after the very first tick (gates are trivially
    # met when nothing requires evidence), defeating the point of driving
    # several ticks here.
    criterion = AcceptanceCriterion(
        id="c1", description="implement_exec's own work", verification_method="automated_test"
    )
    req = make_request(acceptance_criteria=[criterion])
    t1 = make_task(
        "t1",
        node_type="implement_exec",
        node_kind=NodeKind.EXECUTION,
        permission_profile="code_edit_default",
        acceptance_criterion_ids=["c1"],
    )
    graph = make_graph([t1])

    run = start_run(req, graph, store)
    for _ in range(3):
        run = controller_tick(run.id, store, deps)
        assert run.status == RunStatus.ACTIVE

    attempts = store.load_attempts(run.id)
    assert len(attempts) == 3
    assert all(a.status == AttemptStatus.INTERRUPTED_FOR_SWAP for a in attempts)
