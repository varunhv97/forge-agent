from __future__ import annotations

from datetime import datetime, timezone

from forge.controller import db
from forge.controller.decision_dispatch import DEFAULT_FALLBACK_POLICIES
from forge.controller.mechanical import (
    MechanicalResult,
    MechanicalRunnerRegistry,
    make_shell_runner,
)
from forge.controller.scheduler import (
    ControllerDependencies,
    reconcile_orphaned_attempts,
    run_to_completion,
    start_run,
)
from forge.controller.store import Store
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.decision import DecisionAction, DecisionProposal
from forge.models.request import AcceptanceCriterion
from forge.models.run import RunStatus


def _mechanical_registry() -> MechanicalRunnerRegistry:
    registry = MechanicalRunnerRegistry()
    registry.register("shell_ok", make_shell_runner(["true"]))
    registry.register("shell_fail", make_shell_runner(["false"]))
    return registry


def _deps() -> ControllerDependencies:
    """
    M1-era tests exercise only mechanical dispatch directly — no live
    decision adapter, so decision_fn stays None and any task failure falls
    through to the deterministic fallback policy registered by default.
    """
    return ControllerDependencies(mechanical_registry=_mechanical_registry())


def test_linear_chain_completes(store: Store, request_factory, task_factory, graph_factory):
    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion])
    t1 = task_factory("t1", node_type="shell_ok", node_kind=NodeKind.MECHANICAL)
    t2 = task_factory(
        "t2",
        node_type="shell_ok",
        node_kind=NodeKind.MECHANICAL,
        depends_on=["t1"],
        acceptance_criterion_ids=["c1"],
    )
    graph = graph_factory([t1, t2])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _deps())

    assert result.status == RunStatus.COMPLETED_VERIFIED
    attempts = store.load_attempts(run.id)
    assert {a.task_id: a.status for a in attempts} == {
        "t1": AttemptStatus.PASSED,
        "t2": AttemptStatus.PASSED,
    }


def test_parallel_independent_tasks_both_dispatch(
    store: Store, request_factory, task_factory, graph_factory
):
    t1 = task_factory("t1", node_type="shell_ok", node_kind=NodeKind.MECHANICAL)
    t2 = task_factory("t2", node_type="shell_ok", node_kind=NodeKind.MECHANICAL)
    graph = graph_factory([t1, t2])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _deps())

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert len(store.load_attempts(run.id)) == 2


def test_failed_required_task_marks_run_failed_exhausted(
    store: Store, request_factory, task_factory, graph_factory
):
    t1 = task_factory("t1", node_type="shell_fail", node_kind=NodeKind.MECHANICAL, required=True)
    graph = graph_factory([t1])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _deps())

    assert result.status == RunStatus.FAILED_EXHAUSTED


def test_execution_kind_task_never_dispatches_in_v1(
    store: Store, request_factory, task_factory, graph_factory
):
    """
    v1's scheduler has no LLM-driven adapter yet. An EXECUTION-kind task
    must stall the run, never silently execute as if it were mechanical.
    """
    t1 = task_factory("t1", node_type="implement_exec", node_kind=NodeKind.EXECUTION)
    graph = graph_factory([t1])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _deps())

    assert result.status == RunStatus.ACTIVE  # stalled, not completed, not crashed
    assert store.load_attempts(run.id) == []


def test_passing_attempt_without_matching_evidence_does_not_complete_run(
    store: Store, request_factory, task_factory, graph_factory
):
    """
    A task can pass without that satisfying a required acceptance criterion
    if its acceptance_criterion_ids doesn't actually name that criterion —
    completion is derived from Evidence tied to the criterion, never from a
    task merely having succeeded.
    """
    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion])
    # t1 passes but is never wired to criterion "c1"
    t1 = task_factory("t1", node_type="shell_ok", node_kind=NodeKind.MECHANICAL)
    graph = graph_factory([t1])

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _deps())

    assert result.status == RunStatus.ACTIVE
    assert store.load_attempts(run.id)[0].status == AttemptStatus.PASSED


def test_crash_mid_dispatch_resumes_without_duplicate_or_lost_work(
    db_path, request_factory, task_factory, graph_factory
):
    """
    The M1 resumability guarantee: simulate a controller process that died
    right after marking an attempt RUNNING (durably written, never
    finished). A fresh process pointed at the same database must reconcile
    that orphan and still drive the run to completion, with exactly one
    successful attempt per task — never two concurrently-live attempts for
    the same task, and never a task silently skipped.
    """
    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion])
    t1 = task_factory("t1", node_type="shell_ok", node_kind=NodeKind.MECHANICAL)
    t2 = task_factory(
        "t2",
        node_type="shell_ok",
        node_kind=NodeKind.MECHANICAL,
        depends_on=["t1"],
        acceptance_criterion_ids=["c1"],
    )
    graph = graph_factory([t1, t2])

    # --- "process 1": starts the run, dispatches t1, then dies mid-flight ---
    conn1 = db.connect(db_path)
    store1 = Store(conn1)
    run = start_run(req, graph, store1)
    orphaned_attempt = Attempt(
        id="att-orphan",
        run_id=run.id,
        task_id="t1",
        graph_version_id=graph.id,
        attempt_number=1,
        model_used="mechanical",
        permission_profile="default",
        worktree_path="",
        branch_name="",
        base_commit="",
        status=AttemptStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
    )
    store1.save_attempt(orphaned_attempt)
    conn1.close()  # simulates the process dying — no clean shutdown, no final status

    # --- "process 2": fresh connection to the same file, resumes the run ---
    conn2 = db.connect(db_path)
    store2 = Store(conn2)
    reconciled_count = reconcile_orphaned_attempts(store2, run.id)
    assert reconciled_count == 1

    result = run_to_completion(run.id, store2, _deps())
    conn2.close()

    assert result.status == RunStatus.COMPLETED_VERIFIED

    conn3 = db.connect(db_path)
    store3 = Store(conn3)
    attempts = store3.load_attempts(run.id)
    conn3.close()

    t1_attempts = [a for a in attempts if a.task_id == "t1"]
    assert [a.status for a in t1_attempts] == [AttemptStatus.CANCELLED, AttemptStatus.PASSED]
    assert sum(1 for a in t1_attempts if a.status == AttemptStatus.PASSED) == 1

    t2_attempts = [a for a in attempts if a.task_id == "t2"]
    assert len(t2_attempts) == 1
    assert t2_attempts[0].status == AttemptStatus.PASSED


# --- M3: decision-driven retry/escalation on failure ----------------------


def _flaky_runner(fail_times: int):
    """A mechanical runner that fails `fail_times` times, then passes."""
    calls = {"count": 0}

    def runner(_task):
        calls["count"] += 1
        if calls["count"] <= fail_times:
            return MechanicalResult(passed=False, summary=f"flaky failure #{calls['count']}")
        return MechanicalResult(passed=True, summary="finally ok")

    return runner


def test_failed_task_retries_via_fallback_then_succeeds(
    store: Store, request_factory, task_factory, graph_factory
):
    registry = MechanicalRunnerRegistry()
    registry.register("flaky", _flaky_runner(fail_times=2))
    deps = ControllerDependencies(mechanical_registry=registry)

    t1 = task_factory("t1", node_type="flaky", node_kind=NodeKind.MECHANICAL)
    graph = graph_factory([t1])
    req = request_factory()  # default max_retries_per_task=3, enough for 2 failures + 1 success

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED
    attempts = sorted(store.load_attempts(run.id), key=lambda a: a.attempt_number)
    assert [a.status for a in attempts] == [
        AttemptStatus.FAILED,
        AttemptStatus.FAILED,
        AttemptStatus.PASSED,
    ]
    # each retry is a new forward-pointing attempt, never a mutation of the prior one
    assert attempts[1].retries_from_attempt_id == attempts[0].id
    assert attempts[2].retries_from_attempt_id == attempts[1].id


def test_failed_task_exhausts_budget_and_stops_blocked_via_decision(
    store: Store, request_factory, task_factory, graph_factory
):
    """
    Distinguishes the new decision-driven path from the old M1 safety net:
    confirms actual retries happened (multiple attempts, a STOP_BLOCKED
    decision trace) rather than the task just immediately being marked
    failed with no retry activity at all.
    """
    from forge.models.permissions import Budgets

    registry = _mechanical_registry()
    deps = ControllerDependencies(mechanical_registry=registry)

    t1 = task_factory("t1", node_type="shell_fail", node_kind=NodeKind.MECHANICAL, required=True)
    graph = graph_factory([t1])
    req = request_factory()
    req = req.model_copy(
        update={"budgets": Budgets(max_cost_usd=1, max_wall_seconds=1, max_retries_per_task=2)}
    )

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.FAILED_EXHAUSTED
    attempts = store.load_attempts(run.id)
    assert len(attempts) == 2  # exactly max_retries_per_task attempts, no more
    decision_events = [
        e
        for e in store.load_trace_events(run.id)
        if e.event_type == "decision_applied" and e.payload.get("outcome") == "failed_exhausted"
    ]
    assert len(decision_events) == 1


def test_live_decision_fn_overrides_fallback(
    store: Store, request_factory, task_factory, graph_factory
):
    registry = _mechanical_registry()

    def always_escalate(node_type, context):
        return DecisionProposal(
            id=f"dec-{context['attempt_count_for_task']}",
            decision_node_type=node_type,
            action=DecisionAction.ESCALATE_MODEL,
            target_task_ids=[context["task_id"]],
            escalate_to_model="claude-opus-5",
            confidence=0.9,
            evidence_refs=["ev-live"],
            model_used="claude-sonnet-5",
            created_at=datetime.now(timezone.utc),
        )

    from forge.controller.model_registry import ModelRegistry
    from forge.models.model_registry import ModelCapability

    model_registry = ModelRegistry(
        [
            ModelCapability(
                provider="anthropic",
                model_id="claude-opus-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=200000,
                cost_per_mtok_in=15,
                cost_per_mtok_out=75,
                eligible_roles=["shell_fail"],
            )
        ]
    )
    deps = ControllerDependencies(
        mechanical_registry=registry, model_registry=model_registry, decision_fn=always_escalate
    )

    t1 = task_factory("t1", node_type="shell_fail", node_kind=NodeKind.MECHANICAL, required=True)
    graph = graph_factory([t1])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    # shell_fail always fails regardless of model_used, so this still ends
    # up FAILED_EXHAUSTED once the (live, not fallback) decision's own
    # escalation attempts run out — the point is confirming the live
    # decision_fn's escalation was actually used, not the fallback's retry.
    assert result.status == RunStatus.FAILED_EXHAUSTED
    attempts = sorted(store.load_attempts(run.id), key=lambda a: a.attempt_number)
    assert attempts[0].model_used == "mechanical"  # the original dispatch, not yet escalated
    assert all(a.model_used == "claude-opus-5" for a in attempts[1:])


def test_decision_fn_failure_falls_back_cleanly(
    store: Store, request_factory, task_factory, graph_factory
):
    registry = MechanicalRunnerRegistry()
    registry.register("flaky", _flaky_runner(fail_times=1))

    def broken_decision_fn(node_type, context):
        raise RuntimeError("simulated API outage")

    deps = ControllerDependencies(mechanical_registry=registry, decision_fn=broken_decision_fn)

    t1 = task_factory("t1", node_type="flaky", node_kind=NodeKind.MECHANICAL)
    graph = graph_factory([t1])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, deps)

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert len(store.load_attempts(run.id)) == 2  # fallback retried once, then it passed
