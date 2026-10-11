from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional

from forge.controller.decision_dispatch import (
    DEFAULT_FALLBACK_POLICIES,
    DecisionFn,
    FallbackFn,
    invoke_decision_node,
)
from forge.controller.decisions import RejectedProposalError, apply_decision
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.store import Store
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.evidence import Evidence
from forge.models.graph import GraphVersion, TaskSpec
from forge.models.request import Request
from forge.models.run import Run, RunStatus
from forge.models.trace import TraceEvent

# A safety cap on how many decision rounds a single controller_tick will
# drive a run's currently-failing tasks through, independent of any single
# task's own retry budget. Guards against a configuration bug (e.g. several
# tasks each with a large budget) turning one tick into an unbounded loop.
MAX_DECISION_ROUNDS_PER_TICK = 50


@dataclass
class ControllerDependencies:
    """
    Everything controller_tick needs beyond the store itself, bundled so
    the function signature doesn't grow a new parameter every time a new
    kind of node or decision gets wired in.
    """

    mechanical_registry: MechanicalRunnerRegistry
    node_registry: NodeTypeRegistry = field(default_factory=NodeTypeRegistry)
    permission_registry: PermissionProfileRegistry = field(
        default_factory=PermissionProfileRegistry
    )
    model_registry: ModelRegistry = field(default_factory=lambda: ModelRegistry([]))
    decision_fn: Optional[DecisionFn] = None
    fallback_policies: Dict[str, FallbackFn] = field(
        default_factory=lambda: dict(DEFAULT_FALLBACK_POLICIES)
    )


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def start_run(request: Request, graph: GraphVersion, store: Store) -> Run:
    """
    Persists a request and its compiled graph, and creates the run that
    will execute it. The caller is responsible for having already produced
    `graph` via `compile_and_validate` — this function does not validate
    the graph itself, it only persists and schedules against it.
    """
    store.save_request(request)
    store.save_graph_version(graph)
    run = Run(
        id=new_id("run"),
        request_id=request.id,
        active_graph_version_id=graph.id,
        status=RunStatus.ACTIVE,
        created_at=_now(),
        updated_at=_now(),
    )
    store.save_run(run)
    return run


def reconcile_orphaned_attempts(store: Store, run_id: str) -> int:
    """
    Called once before a run resumes. This is a single-process execution
    model: any attempt still marked RUNNING in the database was, by
    definition, abandoned by a prior process that died before recording a
    result — no other process could still be executing it. Cancelling it
    makes its task look not-yet-attempted again, so the scheduler dispatches
    a fresh attempt rather than waiting forever on one that will never
    report back. Returns the number of attempts reconciled.
    """
    reconciled = 0
    for attempt in store.load_attempts(run_id):
        if attempt.status != AttemptStatus.RUNNING:
            continue
        attempt.status = AttemptStatus.CANCELLED
        attempt.finished_at = _now()
        attempt.failure_summary = "cancelled: orphaned by a prior controller process"
        store.update_attempt(attempt)
        reconciled += 1
    return reconciled


def _ready_tasks(graph: GraphVersion, attempts: list[Attempt]) -> list[TaskSpec]:
    attempts_by_task: dict[str, list[Attempt]] = {}
    for a in attempts:
        attempts_by_task.setdefault(a.task_id, []).append(a)

    ready: list[TaskSpec] = []
    for task in graph.tasks:
        if task.node_kind != NodeKind.MECHANICAL:
            # Execution nodes need an LLM-driven adapter (Claude Agent SDK /
            # Codex CLI), which doesn't exist yet. Until that adapter layer
            # is wired in, a non-mechanical task simply never becomes ready
            # — it stalls the run rather than being silently skipped.
            continue
        existing = attempts_by_task.get(task.id, [])
        if any(a.status in (AttemptStatus.RUNNING, AttemptStatus.PASSED) for a in existing):
            continue
        if any(a.status == AttemptStatus.FAILED for a in existing):
            # A failed task is never silently re-dispatched by this generic
            # readiness check — the only path back to running is an
            # explicit RETRY_WITH_FEEDBACK/ESCALATE_MODEL decision, handled
            # separately in _resolve_failed_tasks.
            continue
        if all(
            any(a.task_id == dep and a.status == AttemptStatus.PASSED for a in attempts)
            for dep in task.depends_on
        ):
            ready.append(task)
    return ready


def _latest_attempt_per_task(attempts: list[Attempt]) -> dict[str, Attempt]:
    latest: dict[str, Attempt] = {}
    for a in attempts:
        current = latest.get(a.task_id)
        if current is None or a.attempt_number > current.attempt_number:
            latest[a.task_id] = a
    return latest


def _failed_tasks_needing_decision(
    graph: GraphVersion, attempts: list[Attempt]
) -> list[tuple[TaskSpec, Attempt]]:
    task_by_id = {t.id: t for t in graph.tasks}
    result: list[tuple[TaskSpec, Attempt]] = []
    for task_id, attempt in _latest_attempt_per_task(attempts).items():
        if attempt.status == AttemptStatus.FAILED and task_id in task_by_id:
            result.append((task_by_id[task_id], attempt))
    return result


def _has_unrecoverable_failure(graph: GraphVersion, attempts: list[Attempt]) -> bool:
    """
    True iff some required task's *latest* attempt is FAILED — not merely
    "has ever failed." Attempt history is append-only, so a task that
    failed once and then succeeded on retry still has an old FAILED row;
    only the most recent attempt tells you whether it's actually stuck.
    """
    task_by_id = {t.id: t for t in graph.tasks}
    latest = _latest_attempt_per_task(attempts)
    return any(
        task_by_id[task_id].required
        for task_id, attempt in latest.items()
        if attempt.status == AttemptStatus.FAILED and task_id in task_by_id
    )


def all_required_gates_met(
    graph: GraphVersion,
    request: Request,
    evidence: list[Evidence],
) -> bool:
    """
    The controller's own recomputation of completion — never a decision
    node's opinion. Derived solely from persisted, non-invalidated Evidence
    rows tied to required tasks and required acceptance criteria.
    """
    live_evidence_by_task: dict[str, list[Evidence]] = {}
    for e in evidence:
        if e.invalidated_by_evidence_id is None:
            live_evidence_by_task.setdefault(e.task_id, []).append(e)

    for criterion in request.acceptance_criteria:
        if not criterion.required:
            continue
        satisfied = False
        for task in graph.tasks:
            if not task.required or criterion.id not in task.acceptance_criterion_ids:
                continue
            if any(
                e.acceptance_criterion_id == criterion.id and e.passed
                for e in live_evidence_by_task.get(task.id, [])
            ):
                satisfied = True
                break
        if not satisfied:
            return False
    return True


def dispatch_mechanical_attempt(
    task: TaskSpec,
    run: Run,
    attempt_number: int,
    store: Store,
    mechanical_registry: MechanicalRunnerRegistry,
    *,
    model_used: str = "mechanical",
    retries_from_attempt_id: Optional[str] = None,
    escalated_from_attempt_id: Optional[str] = None,
) -> Attempt:
    """
    Creates and synchronously runs one attempt for `task`. Used both by the
    normal dependency-driven readiness loop (a fresh task, no retry/escalate
    lineage) and by decision handling (a forward-pointing retry/escalation
    attempt) — the only difference is the optional lineage/model fields.
    """
    attempt = Attempt(
        id=new_id("att"),
        run_id=run.id,
        task_id=task.id,
        graph_version_id=run.active_graph_version_id,
        attempt_number=attempt_number,
        retries_from_attempt_id=retries_from_attempt_id,
        escalated_from_attempt_id=escalated_from_attempt_id,
        model_used=model_used,
        permission_profile=task.permission_profile,
        worktree_path="",
        branch_name="",
        base_commit="",
        status=AttemptStatus.RUNNING,
        started_at=_now(),
    )
    # Durable before execution: if the process dies right after this write,
    # the attempt is left RUNNING for reconcile_orphaned_attempts to find
    # and cancel on the next startup — never silently lost.
    store.save_attempt(attempt)

    runner = mechanical_registry.get(task.node_type)
    result = runner(task)

    attempt.status = AttemptStatus.PASSED if result.passed else AttemptStatus.FAILED
    attempt.finished_at = _now()
    attempt.failure_summary = None if result.passed else result.summary

    evidence = [
        Evidence(
            id=new_id("ev"),
            run_id=run.id,
            attempt_id=attempt.id,
            task_id=task.id,
            acceptance_criterion_id=criterion_id,
            kind="test_result",
            passed=result.passed,
            commit_sha="",
            raw_artifact_id=attempt.id,
            created_at=_now(),
        )
        for criterion_id in task.acceptance_criterion_ids
    ]
    store.record_attempt_result(attempt, evidence)
    store.append_trace_event(
        TraceEvent(
            id=new_id("tev"),
            run_id=run.id,
            event_type="attempt_finished",
            payload={
                "task_id": task.id,
                "attempt_id": attempt.id,
                "status": attempt.status,
                "summary": result.summary,
            },
            created_at=_now(),
        )
    )
    return attempt


def _decision_context_for_failure(
    task: TaskSpec, failed_attempt: Attempt, attempts: list[Attempt], request: Request
) -> dict:
    attempt_count = sum(1 for a in attempts if a.task_id == task.id)
    return {
        "task_id": task.id,
        "node_type_name": task.node_type,
        "attempt_count_for_task": attempt_count,
        "max_retries_per_task": request.budgets.max_retries_per_task,
        "failure_summary": failed_attempt.failure_summary,
        "evidence_refs": [],
    }


def _resolve_failed_tasks(
    run: Run,
    graph: GraphVersion,
    request: Request,
    store: Store,
    deps: ControllerDependencies,
) -> Run:
    """
    Drives every currently-failing task through a decision (retry,
    escalate, or give up) until either nothing is failing any more, the run
    leaves ACTIVE, or a round makes no progress at all (every proposal this
    round was rejected). Bounded by MAX_DECISION_ROUNDS_PER_TICK so a
    configuration bug can't turn this into an unbounded loop; the ordinary
    per-task retry budget is what's expected to end things long before that
    cap is ever reached.
    """

    def dispatch_retry(
        task, run, *, model_used, retries_from_attempt_id, escalated_from_attempt_id
    ):
        attempts = store.load_attempts(run.id)
        attempt_number = 1 + sum(1 for a in attempts if a.task_id == task.id)
        return dispatch_mechanical_attempt(
            task,
            run,
            attempt_number,
            store,
            deps.mechanical_registry,
            model_used=model_used,
            retries_from_attempt_id=retries_from_attempt_id,
            escalated_from_attempt_id=escalated_from_attempt_id,
        )

    for _ in range(MAX_DECISION_ROUNDS_PER_TICK):
        attempts = store.load_attempts(run.id)
        failing = _failed_tasks_needing_decision(graph, attempts)
        if not failing:
            break

        made_progress = False
        for task, failed_attempt in failing:
            if run.status != RunStatus.ACTIVE:
                break
            context = _decision_context_for_failure(
                task, failed_attempt, store.load_attempts(run.id), request
            )
            proposal = invoke_decision_node(
                "retry_or_escalate_policy", context, deps.decision_fn, deps.fallback_policies
            )
            try:
                run = apply_decision(
                    run,
                    graph,
                    request,
                    proposal,
                    store=store,
                    node_registry=deps.node_registry,
                    permission_registry=deps.permission_registry,
                    model_registry=deps.model_registry,
                    dispatch_retry=dispatch_retry,
                )
                made_progress = True
            except RejectedProposalError:
                store.append_trace_event(
                    TraceEvent(
                        id=new_id("tev"),
                        run_id=run.id,
                        event_type="proposal_rejected",
                        payload={"task_id": task.id, "action": proposal.action},
                        created_at=_now(),
                    )
                )
            graph = store.load_graph_version(run.active_graph_version_id)

        if run.status != RunStatus.ACTIVE:
            break
        if not made_progress:
            # Every proposal this round was rejected — looping again would
            # just repeat the same rejections. Leave the remaining failures
            # for the pre-existing unrecoverable-failure check below.
            break

    return run


def controller_tick(
    run_id: str,
    store: Store,
    deps: ControllerDependencies,
) -> Run:
    """
    One scheduling pass: dispatch everything currently ready, drive any
    currently-failing tasks through a decision, then recompute the run's
    terminal status from what actually happened — never from what a task or
    a decision proposal claims about itself.
    """
    run = store.load_run(run_id)
    if run.status != RunStatus.ACTIVE:
        return run

    request = store.load_request(run.request_id)
    graph = store.load_graph_version(run.active_graph_version_id)
    attempts = store.load_attempts(run_id)

    for task in _ready_tasks(graph, attempts):
        attempt_number = 1 + sum(1 for a in attempts if a.task_id == task.id)
        dispatched = dispatch_mechanical_attempt(
            task, run, attempt_number, store, deps.mechanical_registry
        )
        attempts.append(dispatched)

    run = _resolve_failed_tasks(run, graph, request, store, deps)
    graph = store.load_graph_version(run.active_graph_version_id)
    attempts = store.load_attempts(run_id)

    if run.status == RunStatus.ACTIVE:
        # A failed required task takes priority over gate-checking: gates
        # being "met" can otherwise be vacuously true (no acceptance
        # criterion happens to reference the failed task), which must never
        # be read as success.
        if _has_unrecoverable_failure(graph, attempts):
            run.status = RunStatus.FAILED_EXHAUSTED
            run.updated_at = _now()
            store.update_run(run)
        else:
            evidence = store.load_evidence(run_id)
            if all_required_gates_met(graph, request, evidence):
                run.status = RunStatus.COMPLETED_VERIFIED
                run.updated_at = _now()
                store.update_run(run)

    return run


def _can_make_progress(graph: GraphVersion, attempts: list[Attempt]) -> bool:
    return bool(_ready_tasks(graph, attempts)) or bool(
        _failed_tasks_needing_decision(graph, attempts)
    )


def run_to_completion(
    run_id: str,
    store: Store,
    deps: ControllerDependencies,
    max_ticks: int = 1000,
) -> Run:
    """
    Drives `controller_tick` until the run reaches a terminal state or
    stalls (nothing ready and nothing failing that a decision could still
    act on). Reconciles any orphaned attempts first, so this is also the
    right entry point for resuming a run after a restart.
    """
    reconcile_orphaned_attempts(store, run_id)
    for _ in range(max_ticks):
        run = store.load_run(run_id)
        if run.status != RunStatus.ACTIVE:
            return run
        graph = store.load_graph_version(run.active_graph_version_id)
        attempts = store.load_attempts(run_id)
        if not _can_make_progress(graph, attempts):
            return run
        run = controller_tick(run_id, store, deps)
    raise RuntimeError(f"run '{run_id}' did not reach a terminal state within {max_ticks} ticks")
