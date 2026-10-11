from __future__ import annotations

from datetime import datetime, timezone

import pytest

from forge.controller.decisions import (
    RejectedProposalError,
    apply_decision,
    assert_new_information,
    check_revision_budget,
    materialize_revision,
)
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.decision import DecisionAction, DecisionProposal, GraphRevisionProposal
from forge.models.evidence import Evidence
from forge.models.permissions import Budgets, PermissionProfile
from forge.models.run import Run, RunStatus


def _now():
    return datetime.now(timezone.utc)


def _make_run(req, graph, store) -> Run:
    run = Run(
        id="run-1",
        request_id=req.id,
        active_graph_version_id=graph.id,
        status=RunStatus.ACTIVE,
        created_at=_now(),
        updated_at=_now(),
    )
    store.save_run(run)
    return run


def _attempt(
    task_id="t1",
    attempt_id="att-1",
    attempt_number=1,
    status=AttemptStatus.FAILED,
    failure_summary="boom",
) -> Attempt:
    return Attempt(
        id=attempt_id,
        run_id="run-1",
        task_id=task_id,
        graph_version_id="gv-1",
        attempt_number=attempt_number,
        model_used="mechanical",
        permission_profile="default",
        worktree_path="",
        branch_name="",
        base_commit="",
        status=status,
        failure_summary=failure_summary,
    )


def _proposal(action, **overrides) -> DecisionProposal:
    defaults = dict(
        id="dec-1",
        decision_node_type="retry_or_escalate_policy",
        action=action,
        target_task_ids=["t1"],
        evidence_refs=["ev-1"],
        confidence=0.5,
        model_used="test-model",
        created_at=_now(),
    )
    defaults.update(overrides)
    return DecisionProposal(**defaults)


class _FakeModelRegistry:
    def __init__(self, eligible: bool = True):
        self.eligible = eligible
        self.calls = []

    def validate_eligible(self, model_id, node_type_name):
        self.calls.append((model_id, node_type_name))
        if not self.eligible:
            raise ValueError(f"'{model_id}' not eligible for '{node_type_name}'")
        return {"model_id": model_id}


class _RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def __call__(
        self, task, run, *, model_used, retries_from_attempt_id, escalated_from_attempt_id
    ):
        self.calls.append(
            dict(
                task_id=task.id,
                model_used=model_used,
                retries_from_attempt_id=retries_from_attempt_id,
                escalated_from_attempt_id=escalated_from_attempt_id,
            )
        )


# --- assert_new_information ----------------------------------------------


def test_assert_new_information_requires_evidence():
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK, evidence_refs=[])
    with pytest.raises(RejectedProposalError, match="evidence"):
        assert_new_information(
            proposal, _attempt(), attempt_count_for_task=1, max_retries_per_task=3
        )


def test_assert_new_information_enforces_budget():
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK)
    with pytest.raises(RejectedProposalError, match="budget exhausted"):
        assert_new_information(
            proposal, _attempt(), attempt_count_for_task=3, max_retries_per_task=3
        )


def test_assert_new_information_rejects_identical_feedback():
    prior = _attempt(failure_summary="same old problem")
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK, retry_feedback="same old problem")
    with pytest.raises(RejectedProposalError, match="no new information"):
        assert_new_information(proposal, prior, attempt_count_for_task=1, max_retries_per_task=3)


def test_assert_new_information_allows_different_feedback():
    prior = _attempt(failure_summary="old problem")
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK, retry_feedback="a new problem")
    assert_new_information(proposal, prior, attempt_count_for_task=1, max_retries_per_task=3)


# --- apply_decision: SCHEDULE_ELIGIBLE ------------------------------------


def test_schedule_eligible_is_a_no_op(store, request_factory, task_factory, graph_factory):
    req = request_factory()
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    proposal = _proposal(DecisionAction.SCHEDULE_ELIGIBLE, evidence_refs=[])
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    assert result.status == RunStatus.ACTIVE


# --- apply_decision: RETRY_WITH_FEEDBACK / ESCALATE_MODEL -----------------


def test_retry_with_feedback_dispatches_forward_attempt(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    store.save_attempt(_attempt())

    dispatcher = _RecordingDispatcher()
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK, retry_feedback="try again differently")
    apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=dispatcher,
    )
    assert dispatcher.calls == [
        {
            "task_id": "t1",
            "model_used": "mechanical",
            "retries_from_attempt_id": "att-1",
            "escalated_from_attempt_id": None,
        }
    ]


def test_retry_with_no_prior_failed_attempt_is_rejected(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    # no attempts saved at all
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK)
    with pytest.raises(RejectedProposalError, match="no FAILED attempt"):
        apply_decision(
            run,
            graph,
            req,
            proposal,
            store=store,
            node_registry=NodeTypeRegistry(),
            permission_registry=PermissionProfileRegistry(),
            model_registry=_FakeModelRegistry(),
            dispatch_retry=_RecordingDispatcher(),
        )


def test_escalate_model_checks_eligibility_before_dispatching(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    graph = graph_factory([task_factory("t1", node_type="root_exec")])
    run = _make_run(req, graph, store)
    store.save_attempt(_attempt())

    model_registry = _FakeModelRegistry(eligible=False)
    proposal = _proposal(DecisionAction.ESCALATE_MODEL, escalate_to_model="claude-opus-5")
    with pytest.raises(ValueError, match="not eligible"):
        apply_decision(
            run,
            graph,
            req,
            proposal,
            store=store,
            node_registry=NodeTypeRegistry(),
            permission_registry=PermissionProfileRegistry(),
            model_registry=model_registry,
            dispatch_retry=_RecordingDispatcher(),
        )
    assert model_registry.calls == [("claude-opus-5", "root_exec")]


def test_escalate_model_dispatches_with_escalated_model_on_success(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    graph = graph_factory([task_factory("t1", node_type="root_exec")])
    run = _make_run(req, graph, store)
    store.save_attempt(_attempt())

    dispatcher = _RecordingDispatcher()
    proposal = _proposal(DecisionAction.ESCALATE_MODEL, escalate_to_model="claude-opus-5")
    apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(eligible=True),
        dispatch_retry=dispatcher,
    )
    assert dispatcher.calls[0]["model_used"] == "claude-opus-5"
    assert dispatcher.calls[0]["escalated_from_attempt_id"] == "att-1"


def test_retry_budget_exhausted_is_rejected_before_dispatch(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory(deploy_authorized=False)
    req = req.model_copy(
        update={"budgets": Budgets(max_cost_usd=1, max_wall_seconds=1, max_retries_per_task=1)}
    )
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    store.save_attempt(_attempt())  # this is already attempt #1; max_retries_per_task=1

    dispatcher = _RecordingDispatcher()
    proposal = _proposal(DecisionAction.RETRY_WITH_FEEDBACK, retry_feedback="new info")
    with pytest.raises(RejectedProposalError, match="budget exhausted"):
        apply_decision(
            run,
            graph,
            req,
            proposal,
            store=store,
            node_registry=NodeTypeRegistry(),
            permission_registry=PermissionProfileRegistry(),
            model_registry=_FakeModelRegistry(),
            dispatch_retry=dispatcher,
        )
    assert dispatcher.calls == []


# --- apply_decision: PROPOSE_GRAPH_REVISION -------------------------------


def test_propose_graph_revision_adds_a_task(
    store, request_factory, task_factory, graph_factory, node_registry, permission_registry
):
    req = request_factory()
    graph = graph_factory([task_factory("t1")], version=1)
    store.save_graph_version(graph)
    run = _make_run(req, graph, store)

    new_task = task_factory("t2")
    revision = GraphRevisionProposal(
        base_graph_version_id=graph.id, add_tasks=[new_task], rationale="need another check"
    )
    proposal = _proposal(
        DecisionAction.PROPOSE_GRAPH_REVISION, graph_revision=revision, target_task_ids=[]
    )
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    new_graph = store.load_graph_version(result.active_graph_version_id)
    assert {t.id for t in new_graph.tasks} == {"t1", "t2"}
    assert new_graph.version == 2


def test_propose_graph_revision_with_invalidated_evidence_supersedes_downstream(
    store, request_factory, task_factory, graph_factory, node_registry, permission_registry
):
    """
    A revision that explicitly invalidates evidence for t1 must: mark that
    evidence row as invalidated, and supersede the latest PASSED attempt of
    every downstream task that depended on it (here, t2) — not just t1
    itself — so the scheduler treats the whole affected chain as needing
    fresh verification rather than silently trusting stale work.
    """
    req = request_factory()
    t1 = task_factory("t1")
    t2 = task_factory("t2", depends_on=["t1"])
    graph = graph_factory([t1, t2], version=1)
    store.save_graph_version(graph)
    run = _make_run(req, graph, store)

    store.save_attempt(_attempt(task_id="t1", attempt_id="att-t1", status=AttemptStatus.PASSED))
    store.save_attempt(_attempt(task_id="t2", attempt_id="att-t2", status=AttemptStatus.PASSED))
    store.save_evidence(
        Evidence(
            id="ev-t1",
            run_id=run.id,
            attempt_id="att-t1",
            task_id="t1",
            kind="test_result",
            passed=True,
            commit_sha="",
            raw_artifact_id="att-t1",
            created_at=_now(),
        )
    )

    revision = GraphRevisionProposal(
        base_graph_version_id=graph.id,
        rationale="t1's evidence turned out to be stale",
        invalidated_evidence_ids=["ev-t1"],
    )
    proposal = _proposal(
        DecisionAction.PROPOSE_GRAPH_REVISION, graph_revision=revision, target_task_ids=[]
    )
    apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )

    reloaded_evidence = store.load_evidence(run.id)
    ev_t1 = next(e for e in reloaded_evidence if e.id == "ev-t1")
    assert ev_t1.invalidated_by_evidence_id == f"decision:{proposal.id}"

    attempts_by_id = {a.id: a for a in store.load_attempts(run.id)}
    assert attempts_by_id["att-t1"].status == AttemptStatus.SUPERSEDED
    assert attempts_by_id["att-t2"].status == AttemptStatus.SUPERSEDED


def test_propose_graph_revision_respects_budget(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    req = req.model_copy(
        update={
            "budgets": Budgets(
                max_cost_usd=1,
                max_wall_seconds=1,
                max_decision_revisions_per_graph_version_lineage=1,
            )
        }
    )
    graph = graph_factory([task_factory("t1")], version=1)
    store.save_graph_version(graph)
    run = _make_run(req, graph, store)

    # simulate one revision already having happened
    store.save_graph_version(
        graph_factory([task_factory("t1"), task_factory("t2")], version=2, parent_version=1)
    )

    revision = GraphRevisionProposal(
        base_graph_version_id=graph.id, add_tasks=[task_factory("t3")], rationale="one more"
    )
    proposal = _proposal(
        DecisionAction.PROPOSE_GRAPH_REVISION, graph_revision=revision, target_task_ids=[]
    )
    with pytest.raises(RejectedProposalError, match="revision budget exhausted"):
        apply_decision(
            run,
            graph,
            req,
            proposal,
            store=store,
            node_registry=NodeTypeRegistry(),
            permission_registry=PermissionProfileRegistry(),
            model_registry=_FakeModelRegistry(),
            dispatch_retry=_RecordingDispatcher(),
        )


# --- apply_decision: REQUEST_USER_INPUT / STOP_SUCCESS / STOP_BLOCKED -----


def test_request_user_input_blocks_the_run(store, request_factory, task_factory, graph_factory):
    req = request_factory()
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    proposal = _proposal(
        DecisionAction.REQUEST_USER_INPUT, user_question="which env?", evidence_refs=[]
    )
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    assert result.status == RunStatus.BLOCKED_AWAITING_INPUT
    assert result.blocker_message == "which env?"


def test_stop_success_rejected_when_gates_unmet(
    store, request_factory, task_factory, graph_factory
):
    from forge.models.request import AcceptanceCriterion

    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion])
    graph = graph_factory([task_factory("t1")])  # t1 doesn't cover c1
    run = _make_run(req, graph, store)
    proposal = _proposal(DecisionAction.STOP_SUCCESS, evidence_refs=[])
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    assert result.status == RunStatus.ACTIVE


def test_stop_success_accepted_when_gates_met(store, request_factory, task_factory, graph_factory):
    from forge.models.request import AcceptanceCriterion

    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="mechanical_check"
    )
    req = request_factory(acceptance_criteria=[criterion])
    t1 = task_factory("t1", acceptance_criterion_ids=["c1"])
    graph = graph_factory([t1])
    run = _make_run(req, graph, store)
    store.save_evidence(
        Evidence(
            id="ev-1",
            run_id=run.id,
            attempt_id="att-1",
            task_id="t1",
            acceptance_criterion_id="c1",
            kind="test_result",
            passed=True,
            commit_sha="",
            raw_artifact_id="att-1",
            created_at=_now(),
        )
    )
    proposal = _proposal(DecisionAction.STOP_SUCCESS, evidence_refs=[])
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    assert result.status == RunStatus.COMPLETED_VERIFIED


def test_stop_blocked_sets_failed_exhausted_with_rationale(
    store, request_factory, task_factory, graph_factory
):
    req = request_factory()
    graph = graph_factory([task_factory("t1")])
    run = _make_run(req, graph, store)
    proposal = _proposal(
        DecisionAction.STOP_BLOCKED, rationale="budget exhausted, giving up", evidence_refs=[]
    )
    result = apply_decision(
        run,
        graph,
        req,
        proposal,
        store=store,
        node_registry=NodeTypeRegistry(),
        permission_registry=PermissionProfileRegistry(),
        model_registry=_FakeModelRegistry(),
        dispatch_retry=_RecordingDispatcher(),
    )
    assert result.status == RunStatus.FAILED_EXHAUSTED
    assert result.blocker_message == "budget exhausted, giving up"


# --- materialize_revision / check_revision_budget -------------------------


def test_materialize_revision_adds_and_removes(task_factory, graph_factory):
    t1, t2 = task_factory("t1"), task_factory("t2")
    base = graph_factory([t1, t2], version=1)
    revision = GraphRevisionProposal(
        base_graph_version_id=base.id,
        add_tasks=[task_factory("t3")],
        remove_task_ids=["t2"],
        rationale="swap t2 for t3",
    )
    draft = materialize_revision(base, revision, new_graph_id="gv-new", created_by="test")
    assert {t.id for t in draft.tasks} == {"t1", "t3"}
    assert draft.parent_version == 1


def test_check_revision_budget_passes_when_under_limit(
    store, request_factory, graph_factory, task_factory
):
    req = request_factory()
    req = req.model_copy(
        update={
            "budgets": Budgets(
                max_cost_usd=1,
                max_wall_seconds=1,
                max_decision_revisions_per_graph_version_lineage=5,
            )
        }
    )
    store.save_graph_version(graph_factory([task_factory("t1")], version=1))
    check_revision_budget(req, store)  # should not raise
