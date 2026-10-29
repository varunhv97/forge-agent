from __future__ import annotations

import queue
from datetime import UTC, datetime
from typing import Protocol

from forge.controller.evidence_index import EvidenceIndex
from forge.controller.execution_registry import EffortSwapRequest
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.store import Store
from forge.controller.validator import compile_and_validate, propagate_invalidation
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import EffortLevel
from forge.models.decision import DecisionAction, DecisionProposal, GraphRevisionProposal
from forge.models.graph import GraphVersion, TaskSpec
from forge.models.request import Request
from forge.models.run import Run, RunStatus
from forge.models.trace import TraceEvent


class RejectedProposalError(Exception):
    """
    Raised when a DecisionProposal is well-formed (it already passed
    Pydantic validation to become a DecisionProposal at all) but fails a
    semantic check the controller enforces on its own authority: a retry
    with no new information, a model outside the task's eligible roles, a
    revision that exceeds its budget, and so on. The proposal is never
    partially applied — either it's accepted in full, or nothing changes.
    """


class ModelEligibilityChecker(Protocol):
    """
    The only thing apply_decision needs from a model registry: validating
    that a proposed escalation target is actually permitted for the task's
    node type. Declared as a Protocol (rather than importing the concrete
    ModelRegistry) so this module has no hard dependency on that class.
    """

    def validate_eligible(self, model_id: str, node_type_name: str) -> object: ...


class DispatchRetryFn(Protocol):
    """
    How apply_decision starts a new attempt for a retried or escalated task.
    Supplied by the scheduler, which owns dispatch; this module only decides
    *whether* to dispatch and with which lineage.
    """

    def __call__(
        self,
        task: TaskSpec,
        run: Run,
        *,
        model_used: str,
        retries_from_attempt_id: str | None,
        escalated_from_attempt_id: str | None,
    ) -> Attempt: ...


class InvalidSwapRequestError(Exception):
    """
    Raised by `request_execution_swap` when a person's live model/effort
    swap request can't be honored: neither a model nor an effort change
    was actually given, the chosen model isn't registered or isn't
    eligible for this task's node type (the same
    `ModelEligibilityChecker.validate_eligible` check `apply_decision`
    already runs for a decision's own `escalate_to_model`), or there's no
    live attempt to swap at all (the queue the request needs doesn't
    exist right now). The TUI surfaces this as a visible notify, never a
    silent no-op.
    """


def request_execution_swap(
    *,
    model_registry: ModelEligibilityChecker,
    node_type_name: str,
    model_swap_queue: queue.Queue[str] | None,
    effort_swap_queue: queue.Queue[EffortSwapRequest] | None,
    model: str | None = None,
    effort: EffortLevel | None = None,
) -> None:
    """
    The one validated path from "a person asked to change a currently-live
    EXECUTION attempt's model/effort" to the actual cross-thread queue
    push(es) that reach it — mirrors `apply_decision`'s own rule that a
    proposal is never trusted to become a real effect until this layer has
    checked it: the TUI (`forge/tui/app.py::ForgeApp.request_execution_swap`,
    called from `forge/tui/screens.py`'s swap modal) never pokes a
    `RunWorker`'s `model_swap_queue`/`effort_swap_queue` directly with
    unvalidated input — this is the only function that does.

    A requested `model` is checked against `model_registry.validate_eligible`
    exactly like `apply_decision` already does for a decision's own
    `escalate_to_model` — unknown or ineligible for `node_type_name` raises
    whatever that check raises (`UnknownModelError`/`ModelNotEligibleError`
    from `forge.controller.model_registry`, not caught or wrapped here).
    `effort` needs no equivalent registry check — same reasoning as every
    other effort value in this codebase: it's already constrained to a
    real `EffortLevel` by its own `Literal` type.

    Routing follows the two genuinely different mechanisms this feature
    is built on: a model-only request (no `effort`) goes through the fast,
    no-interruption path (`model_swap_queue`, just the model string —
    `ClaudeSDKClient.set_model` applies it live, no reconnect). Any request
    that includes an `effort` change (with or without a `model` alongside
    it) goes through the interrupt-and-reconnect path instead
    (`effort_swap_queue`, one bundled `EffortSwapRequest`) — there is no
    in-place way to change effort, so a simultaneous model change rides
    along on the same reconnect rather than costing the attempt a second
    one.

    Raises `InvalidSwapRequestError` when neither `model` nor `effort` is
    given (nothing to do), or when the queue the request actually needs is
    `None` (no live attempt to swap right now — the caller is expected to
    have already checked this the same way the chat input already gates
    on `active_chat_queue_for`, but this is enforced here too rather than
    trusted).
    """
    if model is None and effort is None:
        raise InvalidSwapRequestError("neither a model nor an effort change was requested")
    if model is not None:
        model_registry.validate_eligible(model, node_type_name)

    if effort is not None:
        if effort_swap_queue is None:
            raise InvalidSwapRequestError("no live attempt to swap effort for right now")
        effort_swap_queue.put(EffortSwapRequest(effort=effort, model=model))
        return

    if model_swap_queue is None:
        raise InvalidSwapRequestError("no live attempt to swap model for right now")
    if model is None:
        # Unreachable in practice: the first check above already raised
        # unless at least one of model/effort was given, and the `effort
        # is not None` branch above already returned — so reaching here
        # means effort is None, which means model must be the one that
        # was given. A real `raise` (not `assert`, which Python strips
        # under `-O`) both keeps this safe at runtime regardless and lets
        # mypy narrow `model` from `str | None` to `str` for the `put()`
        # call below, which needs a real `str` for `queue.Queue[str]`.
        raise InvalidSwapRequestError("neither a model nor an effort change was requested")
    model_swap_queue.put(model)


def _now() -> datetime:
    return datetime.now(UTC)


def _latest_attempt_for_task(attempts: list[Attempt], task_id: str) -> Attempt | None:
    matching = [a for a in attempts if a.task_id == task_id]
    if not matching:
        return None
    return max(matching, key=lambda a: a.attempt_number)


def _task_by_id(graph: GraphVersion, task_id: str) -> TaskSpec:
    for task in graph.tasks:
        if task.id == task_id:
            return task
    raise RejectedProposalError(f"proposal targets unknown task '{task_id}'")


def assert_new_information(
    proposal: DecisionProposal,
    prior: Attempt,
    *,
    attempt_count_for_task: int,
    max_retries_per_task: int,
) -> None:
    """
    A retry or escalation must be grounded in evidence, must stay within
    budget, and must not simply repeat the exact feedback that already
    failed once. This is a deliberately simpler, concrete check rather than
    a full history of which evidence a prior decision cited: it compares
    the proposal's own feedback against the prior attempt's own failure
    summary, which is what's actually persisted and available to compare.
    """
    if not proposal.evidence_refs:
        raise RejectedProposalError(f"{proposal.action} must cite at least one evidence ref")
    if attempt_count_for_task >= max_retries_per_task:
        raise RejectedProposalError(
            f"retry budget exhausted for this task ({attempt_count_for_task}/"
            f"{max_retries_per_task} attempts already made)"
        )
    if proposal.retry_feedback is not None and proposal.retry_feedback == prior.failure_summary:
        raise RejectedProposalError(
            "retry feedback is identical to the prior attempt's failure summary — "
            "no new information to act on"
        )


def materialize_revision(
    base: GraphVersion, revision: GraphRevisionProposal, *, new_graph_id: str, created_by: str
) -> GraphVersion:
    """
    Builds the draft GraphVersion a revision proposal describes: the base
    graph's tasks/edges, minus anything in `remove_task_ids`, plus whatever
    `add_tasks`/`add_edges` contribute. The result is a draft — it still has
    to pass `compile_and_validate` before it's trusted, same as any other
    graph version.
    """
    removed = set(revision.remove_task_ids)
    remaining_tasks = [t for t in base.tasks if t.id not in removed]
    remaining_edges = [
        e for e in base.edges if e.from_task not in removed and e.to_task not in removed
    ]
    return GraphVersion(
        id=new_graph_id,
        request_id=base.request_id,
        version=0,  # overwritten by compile_and_validate
        parent_version=base.version,
        tasks=remaining_tasks + revision.add_tasks,
        edges=remaining_edges + revision.add_edges,
        status="draft",
        revision_rationale=revision.rationale,
        created_by=created_by,
        created_at=_now(),
    )


def check_revision_budget(request: Request, store: Store) -> None:
    """
    A graph's revision count (versions beyond the original v1) is capped by
    `Budgets.max_decision_revisions_per_graph_version_lineage`, independent
    of how many times a decision node has tried to propose one — this is
    checked every time, not just the first, so repeated re-proposing can't
    wear the ceiling down.
    """
    versions = store.load_graph_versions(request.id)
    revision_count = max(0, len(versions) - 1)
    if revision_count >= request.budgets.max_decision_revisions_per_graph_version_lineage:
        raise RejectedProposalError(
            f"graph revision budget exhausted ({revision_count}/"
            f"{request.budgets.max_decision_revisions_per_graph_version_lineage} revisions already made)"
        )


def _mark_invalidated_and_supersede(
    store: Store,
    run: Run,
    new_graph: GraphVersion,
    evidence_index: EvidenceIndex,
    invalidated_evidence_ids: frozenset[str],
    invalidated_by: str,
) -> set[str]:
    """
    Marks the explicitly-invalidated evidence rows, derives every
    downstream task whose dependency chain consumed that evidence
    (`propagate_invalidation`), and for each of those tasks: invalidates
    its own live evidence too (its PASSED result may have been produced
    using the now-suspect upstream output, so it can't be trusted either),
    and supersedes its latest PASSED attempt. A superseded attempt doesn't
    block the scheduler's readiness check the way a PASSED one does, so
    the task gets a fresh attempt on the next tick instead of stale work
    being silently trusted — and with its evidence invalidated too, a
    completion check can't quietly keep counting the old result either.
    """
    for evidence_id in invalidated_evidence_ids:
        evidence = evidence_index.get(evidence_id)
        if evidence is None:
            continue
        evidence.invalidated_by_evidence_id = invalidated_by
        store.update_evidence(evidence)

    affected_task_ids = propagate_invalidation(new_graph, evidence_index, invalidated_evidence_ids)

    for task_id in affected_task_ids:
        for evidence in evidence_index.for_task(task_id):
            if evidence.invalidated_by_evidence_id is None:
                evidence.invalidated_by_evidence_id = invalidated_by
                store.update_evidence(evidence)

    attempts = store.load_attempts(run.id)
    for task_id in affected_task_ids:
        attempt = _latest_attempt_for_task(attempts, task_id)
        if attempt is not None and attempt.status == AttemptStatus.PASSED:
            attempt.status = AttemptStatus.SUPERSEDED
            store.update_attempt(attempt)
    return affected_task_ids


def _record_decision_trace(
    store: Store, run_id: str, proposal: DecisionProposal, outcome: str
) -> None:
    store.append_trace_event(
        TraceEvent(
            id=f"tev-{proposal.id}",
            run_id=run_id,
            event_type="decision_applied",
            payload={
                "decision_node_type": proposal.decision_node_type,
                "action": proposal.action,
                "target_task_ids": proposal.target_task_ids,
                "model_used": proposal.model_used,
                "outcome": outcome,
            },
            created_at=_now(),
        )
    )


def apply_decision(  # noqa: PLR0912, PLR0915 - one branch per DecisionAction
    run: Run,
    graph: GraphVersion,
    request: Request,
    proposal: DecisionProposal,
    *,
    store: Store,
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    model_registry: ModelEligibilityChecker,
    dispatch_retry: DispatchRetryFn,
) -> Run:
    """
    The only place a DecisionProposal becomes a real state change. Every
    action is validated against the controller's own authority before
    anything is written — a proposal that fails validation raises
    RejectedProposalError and changes nothing.
    """
    attempts = store.load_attempts(run.id)

    if proposal.action == DecisionAction.SCHEDULE_ELIGIBLE:
        _record_decision_trace(store, run.id, proposal, outcome="no_op")
        return run

    if proposal.action in (DecisionAction.RETRY_WITH_FEEDBACK, DecisionAction.ESCALATE_MODEL):
        if not proposal.target_task_ids:
            raise RejectedProposalError(f"{proposal.action} requires at least one target task id")
        task_id = proposal.target_task_ids[0]
        task = _task_by_id(graph, task_id)
        prior = _latest_attempt_for_task(attempts, task_id)
        if prior is None or prior.status != AttemptStatus.FAILED:
            raise RejectedProposalError(
                f"task '{task_id}' has no FAILED attempt to retry or escalate from"
            )
        attempt_count = sum(1 for a in attempts if a.task_id == task_id)
        assert_new_information(
            proposal,
            prior,
            attempt_count_for_task=attempt_count,
            max_retries_per_task=request.budgets.max_retries_per_task,
        )

        if proposal.action == DecisionAction.RETRY_WITH_FEEDBACK:
            dispatch_retry(
                task,
                run,
                model_used=prior.model_used,
                retries_from_attempt_id=prior.id,
                escalated_from_attempt_id=None,
            )
            _record_decision_trace(store, run.id, proposal, outcome="retried")
        else:
            if not proposal.escalate_to_model:
                raise RejectedProposalError("escalate_model requires escalate_to_model to be set")
            model_registry.validate_eligible(proposal.escalate_to_model, task.node_type)
            dispatch_retry(
                task,
                run,
                model_used=proposal.escalate_to_model,
                retries_from_attempt_id=None,
                escalated_from_attempt_id=prior.id,
            )
            _record_decision_trace(store, run.id, proposal, outcome="escalated")
        return store.load_run(run.id)

    if proposal.action == DecisionAction.PROPOSE_GRAPH_REVISION:
        if proposal.graph_revision is None:
            raise RejectedProposalError("propose_graph_revision requires graph_revision to be set")
        check_revision_budget(request, store)
        completed_attempts = store.load_attempts(run.id)
        evidence_index = EvidenceIndex(store.load_evidence(run.id))
        draft = materialize_revision(
            graph,
            proposal.graph_revision,
            new_graph_id=f"gv-{proposal.id}",
            created_by=f"decision_node:{proposal.decision_node_type}",
        )
        new_version = compile_and_validate(
            draft,
            request,
            node_registry,
            permission_registry,
            prior_version=graph,
            completed_attempts=completed_attempts,
            evidence_index=evidence_index,
            invalidated_evidence_ids=frozenset(proposal.graph_revision.invalidated_evidence_ids),
        )
        store.save_graph_version(new_version)
        run.active_graph_version_id = new_version.id
        run.updated_at = _now()
        store.update_run(run)

        if proposal.graph_revision.invalidated_evidence_ids:
            _mark_invalidated_and_supersede(
                store,
                run,
                new_version,
                evidence_index,
                frozenset(proposal.graph_revision.invalidated_evidence_ids),
                invalidated_by=f"decision:{proposal.id}",
            )

        _record_decision_trace(store, run.id, proposal, outcome="revised")
        return run

    if proposal.action == DecisionAction.REQUEST_USER_INPUT:
        run.status = RunStatus.BLOCKED_AWAITING_INPUT
        run.blocker_message = proposal.user_question
        run.updated_at = _now()
        store.update_run(run)
        _record_decision_trace(store, run.id, proposal, outcome="blocked_awaiting_input")
        return run

    if proposal.action == DecisionAction.STOP_SUCCESS:
        # Deferred import: forge.controller.scheduler imports this module
        # (for apply_decision), so importing it back at module load time
        # here would be circular. Importing it lazily, only when this
        # branch actually runs, breaks the cycle.
        from forge.controller.scheduler import all_required_gates_met  # noqa: PLC0415

        evidence = store.load_evidence(run.id)
        if all_required_gates_met(graph, request, evidence):
            run.status = RunStatus.COMPLETED_VERIFIED
            run.updated_at = _now()
            store.update_run(run)
            _record_decision_trace(store, run.id, proposal, outcome="completed")
        else:
            _record_decision_trace(store, run.id, proposal, outcome="rejected_gates_unmet")
        return run

    if proposal.action == DecisionAction.STOP_BLOCKED:
        run.status = RunStatus.FAILED_EXHAUSTED
        run.blocker_message = proposal.rationale
        run.updated_at = _now()
        store.update_run(run)
        _record_decision_trace(store, run.id, proposal, outcome="failed_exhausted")
        return run

    raise RejectedProposalError(f"unhandled decision action: {proposal.action}")
