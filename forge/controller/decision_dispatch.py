from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from forge.models.decision import DecisionAction, DecisionProposal

logger = logging.getLogger(__name__)

# Decision-node model selection is a fixed table, not routed through
# another decision node — this is what stops the "which model decides
# which model decides" regress by fiat rather than by cleverness.
DECISION_MODEL_BY_NODE_TYPE: Dict[str, str] = {
    "retry_or_escalate_policy": "claude-sonnet-5",
}

# A decision function takes (decision_node_type, context) and returns a
# validated DecisionProposal, or raises on any failure (network, malformed
# output, anything) — invoke_decision_node treats every exception the same
# way: fall back to the deterministic policy for that node type.
DecisionFn = Callable[[str, dict], DecisionProposal]
FallbackFn = Callable[[dict], DecisionProposal]


class DecisionUnavailableError(RuntimeError):
    """
    Raised when a decision function failed (or none was configured) and no
    deterministic fallback policy is registered for the given node type
    either — there is nothing left that can produce a proposal.
    """


def _new_id() -> str:
    return f"dec-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def retry_or_escalate_fallback(context: dict) -> DecisionProposal:
    """
    The hand-written, non-LLM default for retry_or_escalate_policy: retry
    once with generic feedback if the task's retry budget allows it,
    otherwise stop the run as blocked rather than looping forever. This is
    what keeps a run from hanging when the real decision adapter is
    unavailable or its output can't be trusted — the run degrades to a
    safe, inspectable state instead.
    """
    attempt_count = context["attempt_count_for_task"]
    max_retries = context["max_retries_per_task"]
    task_id = context["task_id"]
    evidence_refs = context.get("evidence_refs") or ["fallback:no-evidence-tracked"]

    if attempt_count < max_retries:
        return DecisionProposal(
            id=_new_id(),
            decision_node_type="retry_or_escalate_policy",
            action=DecisionAction.RETRY_WITH_FEEDBACK,
            target_task_ids=[task_id],
            retry_feedback=f"deterministic fallback retry (attempt {attempt_count + 1})",
            confidence=0.0,
            evidence_refs=evidence_refs,
            model_used="fallback:none",
            created_at=_now(),
        )
    return DecisionProposal(
        id=_new_id(),
        decision_node_type="retry_or_escalate_policy",
        action=DecisionAction.STOP_BLOCKED,
        target_task_ids=[task_id],
        rationale=(
            f"task '{task_id}' exhausted its retry budget "
            f"({attempt_count}/{max_retries}) with no decision adapter available"
        ),
        confidence=0.0,
        evidence_refs=evidence_refs,
        model_used="fallback:none",
        created_at=_now(),
    )


DEFAULT_FALLBACK_POLICIES: Dict[str, FallbackFn] = {
    "retry_or_escalate_policy": retry_or_escalate_fallback,
}


def invoke_decision_node(
    decision_node_type: str,
    context: dict,
    decision_fn: Optional[DecisionFn],
    fallback_policies: Dict[str, FallbackFn],
) -> DecisionProposal:
    """
    Calls `decision_fn` (if one is configured) for `decision_node_type`. On
    any failure — the function raising for any reason, or none being
    configured at all — falls back to the deterministic policy registered
    for that node type, so a judgment-call failure degrades the run to a
    safe, inspectable state instead of hanging or crashing the controller.
    """
    if decision_fn is not None:
        try:
            return decision_fn(decision_node_type, context)
        except Exception:
            logger.warning(
                "decision function failed for '%s'; falling back to deterministic policy",
                decision_node_type,
                exc_info=True,
            )

    try:
        fallback = fallback_policies[decision_node_type]
    except KeyError as exc:
        raise DecisionUnavailableError(
            f"no decision function succeeded and no fallback policy is registered "
            f"for '{decision_node_type}'"
        ) from exc
    return fallback(context)
