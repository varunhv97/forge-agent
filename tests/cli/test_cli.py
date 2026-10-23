from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from forge.cli import TaskNotFoundError, build_inspect_report, build_trace_report
from forge.controller import db
from forge.controller.node_registry import NodeType, NodeTypeRegistry
from forge.controller.store import NotFoundError, Store
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.evidence import Evidence
from forge.models.request import AcceptanceCriterion
from forge.models.run import Run, RunStatus
from forge.models.trace import TraceEvent


class _EmptyIO(BaseModel):
    pass


def _make_run(
    run_id: str = "run-1",
    request_id: str = "req-1",
    graph_version_id: str = "gv-1",
    status: RunStatus = RunStatus.ACTIVE,
) -> Run:
    now = datetime.now(UTC)
    return Run(
        id=run_id,
        request_id=request_id,
        active_graph_version_id=graph_version_id,
        status=status,
        created_at=now,
        updated_at=now,
    )


def _make_attempt(
    *,
    attempt_id: str,
    run_id: str,
    task_id: str,
    graph_version_id: str,
    attempt_number: int = 1,
    status: AttemptStatus = AttemptStatus.PASSED,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    result_commit: str | None = None,
    outputs: dict[str, object] | None = None,
    failure_summary: str | None = None,
) -> Attempt:
    return Attempt(
        id=attempt_id,
        run_id=run_id,
        task_id=task_id,
        graph_version_id=graph_version_id,
        attempt_number=attempt_number,
        model_used="claude-sonnet-5",
        permission_profile="default",
        worktree_path="/tmp/wt",
        branch_name="b1",
        base_commit="base-sha",
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        result_commit=result_commit,
        outputs=outputs or {},
        failure_summary=failure_summary,
    )


# -- trace --------------------------------------------------------------------


def test_build_trace_report_unknown_run_raises(store: Store) -> None:
    with pytest.raises(NotFoundError):
        build_trace_report(store, "ghost")


def test_build_trace_report_is_chronological_across_event_kinds(
    store: Store, request_factory, task_factory, graph_factory
) -> None:
    """
    Seeds a store with two attempts (dispatch + result), a rejected
    proposal, and a graph-revision decision that invalidated another
    task's evidence — then asserts the rendered trace puts everything in
    the right chronological order and surfaces the invalidation
    propagation the decision caused.
    """
    req = request_factory()
    store.save_request(req)
    t1 = task_factory("implement", node_type="root_exec")
    t2 = task_factory(
        "verify", node_type="verify_exec", node_kind=NodeKind.MECHANICAL, depends_on=["implement"]
    )
    graph = graph_factory([t1, t2], version=1)
    store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    store.save_run(run)

    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    attempt1 = _make_attempt(
        attempt_id="att-1",
        run_id=run.id,
        task_id="implement",
        graph_version_id=graph.id,
        status=AttemptStatus.FAILED,
        started_at=t0,
        finished_at=t0 + timedelta(seconds=5),
        failure_summary="boom",
    )
    store.save_attempt(attempt1)
    store.append_trace_event(
        TraceEvent(
            id="tev-a",
            run_id=run.id,
            event_type="attempt_finished",
            payload={
                "task_id": "implement",
                "attempt_id": "att-1",
                "status": "failed",
                "summary": "boom",
            },
            created_at=t0 + timedelta(seconds=5),
        )
    )
    store.append_trace_event(
        TraceEvent(
            id="tev-b",
            run_id=run.id,
            event_type="proposal_rejected",
            payload={"task_id": "implement", "action": "retry_with_feedback"},
            created_at=t0 + timedelta(seconds=6),
        )
    )

    decision_id = "dec-xyz"
    store.append_trace_event(
        TraceEvent(
            id=f"tev-{decision_id}",
            run_id=run.id,
            event_type="decision_applied",
            payload={
                "decision_node_type": "plan_decision",
                "action": "propose_graph_revision",
                "target_task_ids": [],
                "model_used": "claude-opus-5",
                "outcome": "revised",
            },
            created_at=t0 + timedelta(seconds=10),
        )
    )
    store.save_evidence(
        Evidence(
            id="ev-1",
            run_id=run.id,
            attempt_id="att-1",
            task_id="verify",
            acceptance_criterion_id="c1",
            kind="test_result",
            passed=True,
            commit_sha="base-sha",
            raw_artifact_id="att-1",
            invalidated_by_evidence_id=f"decision:{decision_id}",
            created_at=t0,
        )
    )

    attempt2 = _make_attempt(
        attempt_id="att-2",
        run_id=run.id,
        task_id="implement",
        graph_version_id=graph.id,
        attempt_number=2,
        status=AttemptStatus.RUNNING,
        started_at=t0 + timedelta(seconds=11),
    )
    store.save_attempt(attempt2)

    report = build_trace_report(store, run.id)
    lines = report.splitlines()

    assert lines[0] == f"Trace for run {run.id} (request {req.id}, status=active)"

    def _index_containing(needle: str) -> int:
        return next(i for i, line in enumerate(lines) if needle in line)

    dispatch1 = _index_containing("DISPATCH  task=implement attempt=att-1")
    result1 = _index_containing("RESULT    task=implement attempt=att-1")
    rejected = _index_containing("REJECTED  task=implement action=retry_with_feedback")
    decision = _index_containing("DECISION  plan_decision action=propose_graph_revision")
    dispatch2 = _index_containing("DISPATCH  task=implement attempt=att-2")

    assert dispatch1 < result1 < rejected < decision < dispatch2
    assert "invalidated evidence for tasks: verify" in report


# -- inspect: run id ------------------------------------------------------------


def test_build_inspect_report_unknown_run_raises(store: Store) -> None:
    with pytest.raises(NotFoundError):
        build_inspect_report(store, NodeTypeRegistry(), "ghost")


def test_build_inspect_report_run_id_shows_tasks_and_gate_status(
    store: Store, request_factory, task_factory, graph_factory
) -> None:
    criterion_met = AcceptanceCriterion(
        id="c_met", description="unit tests pass", verification_method="automated_test"
    )
    criterion_unmet = AcceptanceCriterion(
        id="c_unmet", description="the feature is deployed", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion_met, criterion_unmet])
    store.save_request(req)

    t1 = task_factory("implement", node_type="root_exec", acceptance_criterion_ids=["c_met"])
    t2 = task_factory(
        "deploy",
        node_type="deploy_exec",
        node_kind=NodeKind.MECHANICAL,
        depends_on=["implement"],
        acceptance_criterion_ids=["c_unmet"],
    )
    graph = graph_factory([t1, t2], version=1)
    store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    store.save_run(run)

    attempt = _make_attempt(
        attempt_id="att-1",
        run_id=run.id,
        task_id="implement",
        graph_version_id=graph.id,
        status=AttemptStatus.PASSED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    store.save_attempt(attempt)
    store.save_evidence(
        Evidence(
            id="ev-1",
            run_id=run.id,
            attempt_id=attempt.id,
            task_id="implement",
            acceptance_criterion_id="c_met",
            kind="test_result",
            passed=True,
            commit_sha="abc",
            raw_artifact_id=attempt.id,
            created_at=datetime.now(UTC),
        )
    )

    report = build_inspect_report(store, NodeTypeRegistry(), run.id)

    assert f"Run {run.id} — status=active" in report
    assert "implement" in report
    assert "passed (required)" in report
    assert "deploy" in report
    assert "not_started (required)" in report
    assert "[MET] c_met — unit tests pass (satisfied by 'implement')" in report
    assert "[UNMET] c_unmet — the feature is deployed (not yet satisfied)" in report
    assert "Required gates (overall met: False)" in report


# -- inspect: task id ------------------------------------------------------------


def test_build_inspect_report_task_id_commit_diff_strategy(
    store: Store, node_registry: NodeTypeRegistry, request_factory, task_factory, graph_factory
) -> None:
    criterion = AcceptanceCriterion(
        id="c1", description="unit tests pass", verification_method="automated_test"
    )
    req = request_factory(acceptance_criteria=[criterion])
    store.save_request(req)

    t1 = task_factory("implement", node_type="root_exec", acceptance_criterion_ids=["c1"])
    graph = graph_factory([t1], version=1)
    store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    store.save_run(run)

    started = datetime(2026, 1, 1, tzinfo=UTC)
    attempt = _make_attempt(
        attempt_id="att-1",
        run_id=run.id,
        task_id="implement",
        graph_version_id=graph.id,
        status=AttemptStatus.PASSED,
        started_at=started,
        finished_at=started + timedelta(seconds=30),
        result_commit="result-sha",
        outputs={
            "result_commit": "result-sha",
            "changed_paths": ["a.py", "b.py"],
            "summary": "did the thing",
        },
    )
    store.save_attempt(attempt)
    store.save_evidence(
        Evidence(
            id="ev-1",
            run_id=run.id,
            attempt_id=attempt.id,
            task_id="implement",
            acceptance_criterion_id="c1",
            kind="test_result",
            passed=True,
            commit_sha="result-sha",
            touched_paths=["a.py", "b.py"],
            raw_artifact_id=attempt.id,
            created_at=started,
        )
    )

    report = build_inspect_report(store, node_registry, run.id, "implement")

    assert f"Task implement (run {run.id})" in report
    assert "node type:   root_exec (execution), output_strategy=commit_diff" in report
    assert "status:      passed" in report
    assert "#1 att-1 status=passed" in report
    assert "result_commit=result-sha changed_paths=['a.py', 'b.py']" in report
    assert "ev-1 kind=test_result passed=True criterion=c1" in report
    assert "[live]" in report


def test_build_inspect_report_task_id_result_file_strategy(
    store: Store, request_factory, task_factory, graph_factory
) -> None:
    req = request_factory()
    store.save_request(req)

    node_registry = NodeTypeRegistry()
    node_registry.register(
        NodeType(
            name="manual_test_like",
            kind=NodeKind.EXECUTION,
            input_schema=_EmptyIO,
            output_schema=_EmptyIO,
            permission_profile="default",
            output_strategy="result_file",
        )
    )

    t1 = task_factory("manual_test", node_type="manual_test_like")
    graph = graph_factory([t1], version=1)
    store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    store.save_run(run)

    attempt = _make_attempt(
        attempt_id="att-1",
        run_id=run.id,
        task_id="manual_test",
        graph_version_id=graph.id,
        status=AttemptStatus.FAILED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        outputs={"passed": False, "reasoning": "button did nothing", "screenshot_paths": []},
        failure_summary="journey failed",
    )
    store.save_attempt(attempt)

    report = build_inspect_report(store, node_registry, run.id, "manual_test")

    assert "output_strategy=result_file" in report
    assert "result_file: passed=False reasoning='button did nothing'" in report
    assert "failure: journey failed" in report


def test_build_inspect_report_unknown_task_raises(
    store: Store, request_factory, task_factory, graph_factory
) -> None:
    req = request_factory()
    store.save_request(req)
    t1 = task_factory("implement")
    graph = graph_factory([t1], version=1)
    store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    store.save_run(run)

    with pytest.raises(TaskNotFoundError):
        build_inspect_report(store, NodeTypeRegistry(), run.id, "nonexistent")


# -- CLI entry point (scripts/forge_cli.py) --------------------------------------


def test_cli_main_trace_and_inspect(
    tmp_path, capsys: pytest.CaptureFixture[str], request_factory, task_factory, graph_factory
) -> None:
    from scripts.forge_cli import main

    db_path = tmp_path / "forge.db"
    conn = db.connect(db_path)
    seed_store = Store(conn)

    req = request_factory()
    seed_store.save_request(req)
    t1 = task_factory("implement", node_type="implement_exec")
    graph = graph_factory([t1], version=1)
    seed_store.save_graph_version(graph)
    run = _make_run(graph_version_id=graph.id, request_id=req.id)
    seed_store.save_run(run)

    attempt = _make_attempt(
        attempt_id="att-1",
        run_id=run.id,
        task_id="implement",
        graph_version_id=graph.id,
        status=AttemptStatus.PASSED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    seed_store.save_attempt(attempt)
    seed_store.append_trace_event(
        TraceEvent(
            id="tev-1",
            run_id=run.id,
            event_type="attempt_finished",
            payload={
                "task_id": "implement",
                "attempt_id": "att-1",
                "status": "passed",
                "summary": "ok",
            },
            created_at=datetime.now(UTC),
        )
    )
    seed_store.close()

    exit_code = main(["--db", str(db_path), "trace", run.id])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert f"Trace for run {run.id}" in captured.out
    assert "RESULT    task=implement attempt=att-1 status=passed - ok" in captured.out

    exit_code = main(["--db", str(db_path), "inspect", run.id, "implement"])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Task implement" in captured.out
    assert "output_strategy=commit_diff" in captured.out

    exit_code = main(["--db", str(db_path), "inspect", "ghost-run"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err
