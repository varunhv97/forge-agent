from __future__ import annotations

from datetime import datetime, timezone

from forge.controller import db
from forge.controller.mechanical import MechanicalRunnerRegistry, make_shell_runner
from forge.controller.scheduler import (
    reconcile_orphaned_attempts,
    run_to_completion,
    start_run,
)
from forge.controller.store import Store
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.request import AcceptanceCriterion
from forge.models.run import RunStatus


def _mechanical_registry() -> MechanicalRunnerRegistry:
    registry = MechanicalRunnerRegistry()
    registry.register("shell_ok", make_shell_runner(["true"]))
    registry.register("shell_fail", make_shell_runner(["false"]))
    return registry


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
    result = run_to_completion(run.id, store, _mechanical_registry())

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
    result = run_to_completion(run.id, store, _mechanical_registry())

    assert result.status == RunStatus.COMPLETED_VERIFIED
    assert len(store.load_attempts(run.id)) == 2


def test_failed_required_task_marks_run_failed_exhausted(
    store: Store, request_factory, task_factory, graph_factory
):
    t1 = task_factory("t1", node_type="shell_fail", node_kind=NodeKind.MECHANICAL, required=True)
    graph = graph_factory([t1])
    req = request_factory()

    run = start_run(req, graph, store)
    result = run_to_completion(run.id, store, _mechanical_registry())

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
    result = run_to_completion(run.id, store, _mechanical_registry())

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
    result = run_to_completion(run.id, store, _mechanical_registry())

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

    result = run_to_completion(run.id, store2, _mechanical_registry())
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
