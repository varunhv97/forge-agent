from __future__ import annotations

from datetime import UTC, datetime

from forge.controller.evidence_index import EvidenceIndex
from forge.controller.validator import propagate_invalidation
from forge.models.evidence import Evidence


def _evidence(task_id: str, evidence_id: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        run_id="run-1",
        attempt_id="a1",
        task_id=task_id,
        kind="test_result",
        passed=True,
        commit_sha="c1",
        raw_artifact_id="art-1",
        created_at=datetime.now(UTC),
    )


def test_no_invalidated_ids_returns_empty_set(task_factory, graph_factory):
    graph = graph_factory([task_factory("t1")])
    assert propagate_invalidation(graph, EvidenceIndex(), frozenset()) == set()


def test_invalidation_propagates_to_descendants(task_factory, graph_factory):
    t1 = task_factory("t1")
    t2 = task_factory("t2", depends_on=["t1"])
    t3 = task_factory("t3", depends_on=["t2"])
    unrelated = task_factory("t4")
    graph = graph_factory([t1, t2, t3, unrelated])

    evidence_index = EvidenceIndex([_evidence("t1", "e1")])
    affected = propagate_invalidation(graph, evidence_index, frozenset({"e1"}))

    assert affected == {"t1", "t2", "t3"}
    assert "t4" not in affected


def test_invalidation_on_leaf_task_affects_only_itself(task_factory, graph_factory):
    t1 = task_factory("t1")
    t2 = task_factory("t2", depends_on=["t1"])
    graph = graph_factory([t1, t2])

    evidence_index = EvidenceIndex([_evidence("t2", "e1")])
    affected = propagate_invalidation(graph, evidence_index, frozenset({"e1"}))

    assert affected == {"t2"}
