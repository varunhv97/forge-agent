from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge.controller.decision_dispatch import (
    DecisionUnavailableError,
    invoke_decision_node,
    retry_or_escalate_fallback,
)
from forge.models.decision import DecisionAction, DecisionProposal


def _now():
    return datetime.now(UTC)


def _fake_proposal(action=DecisionAction.SCHEDULE_ELIGIBLE) -> DecisionProposal:
    return DecisionProposal(
        id="dec-live",
        decision_node_type="retry_or_escalate_policy",
        action=action,
        confidence=0.9,
        evidence_refs=["ev-1"],
        model_used="claude-sonnet-5",
        created_at=_now(),
    )


def test_live_decision_fn_is_used_when_it_succeeds():
    live_result = _fake_proposal()
    calls = []

    def decision_fn(node_type, context):
        calls.append((node_type, context))
        return live_result

    result = invoke_decision_node(
        "retry_or_escalate_policy",
        {"x": 1},
        decision_fn,
        {"retry_or_escalate_policy": lambda ctx: _fake_proposal()},
    )
    assert result is live_result
    assert calls == [("retry_or_escalate_policy", {"x": 1})]


def test_falls_back_when_decision_fn_raises():
    def decision_fn(node_type, context):
        raise RuntimeError("network exploded")

    fallback_called_with = []

    def fallback(context):
        fallback_called_with.append(context)
        return _fake_proposal(DecisionAction.STOP_BLOCKED)

    result = invoke_decision_node(
        "retry_or_escalate_policy", {"x": 2}, decision_fn, {"retry_or_escalate_policy": fallback}
    )
    assert result.action == DecisionAction.STOP_BLOCKED
    assert fallback_called_with == [{"x": 2}]


def test_falls_back_when_no_decision_fn_configured():
    result = invoke_decision_node(
        "retry_or_escalate_policy",
        {"task_id": "t1", "attempt_count_for_task": 0, "max_retries_per_task": 3},
        None,
        {"retry_or_escalate_policy": retry_or_escalate_fallback},
    )
    assert result.action == DecisionAction.RETRY_WITH_FEEDBACK


def test_raises_when_nothing_can_produce_a_proposal():
    with pytest.raises(DecisionUnavailableError):
        invoke_decision_node("some_unregistered_node_type", {}, None, {})


# --- retry_or_escalate_fallback --------------------------------------------


def test_fallback_retries_within_budget():
    proposal = retry_or_escalate_fallback(
        {"task_id": "t1", "attempt_count_for_task": 1, "max_retries_per_task": 3}
    )
    assert proposal.action == DecisionAction.RETRY_WITH_FEEDBACK
    assert proposal.target_task_ids == ["t1"]
    assert proposal.evidence_refs  # never empty, even with no tracked evidence


def test_fallback_stops_blocked_at_budget():
    proposal = retry_or_escalate_fallback(
        {"task_id": "t1", "attempt_count_for_task": 3, "max_retries_per_task": 3}
    )
    assert proposal.action == DecisionAction.STOP_BLOCKED
    assert proposal.rationale is not None
    assert "t1" in proposal.rationale
