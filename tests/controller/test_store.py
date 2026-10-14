from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge.controller.store import NotFoundError, Store
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.evidence import Evidence
from forge.models.run import Run, RunStatus
from forge.models.trace import TraceEvent


def _attempt(run_id: str = "run-1", task_id: str = "t1", attempt_id: str = "att-1") -> Attempt:
    return Attempt(
        id=attempt_id,
        run_id=run_id,
        task_id=task_id,
        graph_version_id="gv-1",
        attempt_number=1,
        model_used="mechanical",
        permission_profile="default",
        worktree_path="",
        branch_name="",
        base_commit="",
        status=AttemptStatus.RUNNING,
    )


def test_request_roundtrip(store: Store, request_factory):
    req = request_factory()
    store.save_request(req)
    loaded = store.load_request(req.id)
    assert loaded == req


def test_load_missing_request_raises(store: Store):
    with pytest.raises(NotFoundError):
        store.load_request("does-not-exist")


def test_graph_version_roundtrip_and_latest_lookup(store: Store, task_factory, graph_factory):
    g1 = graph_factory([task_factory("t1")], version=1)
    g2 = graph_factory([task_factory("t1"), task_factory("t2")], version=2, parent_version=1)
    store.save_graph_version(g1)
    store.save_graph_version(g2)

    assert store.load_graph_version(g1.id) == g1
    assert store.load_latest_graph_version(g1.request_id) == g2


def test_run_roundtrip_and_update(store: Store):
    run = Run(
        id="run-1",
        request_id="req-1",
        active_graph_version_id="gv-1",
        status=RunStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    store.save_run(run)
    assert store.load_run(run.id).status == RunStatus.ACTIVE

    run.status = RunStatus.COMPLETED_VERIFIED
    store.update_run(run)
    assert store.load_run(run.id).status == RunStatus.COMPLETED_VERIFIED


def test_update_missing_run_raises(store: Store):
    run = Run(
        id="ghost",
        request_id="req-1",
        active_graph_version_id="gv-1",
        status=RunStatus.ACTIVE,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(NotFoundError):
        store.update_run(run)


def test_attempt_save_and_load_by_run(store: Store):
    a1 = _attempt(attempt_id="att-1", task_id="t1")
    a2 = _attempt(attempt_id="att-2", task_id="t2")
    store.save_attempt(a1)
    store.save_attempt(a2)

    loaded = {a.id: a for a in store.load_attempts("run-1")}
    assert set(loaded) == {"att-1", "att-2"}
    assert loaded["att-1"].status == AttemptStatus.RUNNING


def test_record_attempt_result_updates_attempt_and_inserts_evidence_atomically(store: Store):
    attempt = _attempt()
    store.save_attempt(attempt)

    attempt.status = AttemptStatus.PASSED
    evidence = Evidence(
        id="ev-1",
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        acceptance_criterion_id="c1",
        kind="test_result",
        passed=True,
        commit_sha="",
        raw_artifact_id=attempt.id,
        created_at=datetime.now(UTC),
    )
    store.record_attempt_result(attempt, [evidence])

    reloaded = store.load_attempts(attempt.run_id)[0]
    assert reloaded.status == AttemptStatus.PASSED
    stored_evidence = store.load_evidence(attempt.run_id)
    assert len(stored_evidence) == 1
    assert stored_evidence[0].id == "ev-1"


def test_record_attempt_result_on_missing_attempt_raises_and_inserts_nothing(store: Store):
    attempt = _attempt(attempt_id="ghost")
    evidence = Evidence(
        id="ev-1",
        run_id=attempt.run_id,
        attempt_id=attempt.id,
        task_id=attempt.task_id,
        kind="test_result",
        passed=True,
        commit_sha="",
        raw_artifact_id=attempt.id,
        created_at=datetime.now(UTC),
    )
    with pytest.raises(NotFoundError):
        store.record_attempt_result(attempt, [evidence])

    # the transaction must have rolled back the evidence insert too
    assert store.load_evidence(attempt.run_id) == []


def test_trace_events_ordered_by_creation(store: Store):
    first = TraceEvent(
        id="tev-1",
        run_id="run-1",
        event_type="a",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = TraceEvent(
        id="tev-2",
        run_id="run-1",
        event_type="b",
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    store.append_trace_event(second)
    store.append_trace_event(first)

    events = store.load_trace_events("run-1")
    assert [e.id for e in events] == ["tev-1", "tev-2"]
