"""
The controller's scheduling loop: the only code that turns a validated
graph plus its attempt history into new attempts and run-status changes.

`run_to_completion` first reconciles attempts orphaned by a crashed prior
process, then calls `controller_tick` until the run is terminal or stalled.
Each tick:

1. Readiness — `_ready_tasks` finds tasks whose dependencies have passed and
   whose latest attempt is absent, CANCELLED or SUPERSEDED (a FAILED task is
   only ever retried via a decision; EXECUTION tasks are only ready when a
   runner is configured).
2. Dispatch — MECHANICAL tasks go to `dispatch_mechanical_attempt`
   (deterministic runner, no LLM); EXECUTION tasks go to
   `dispatch_execution_attempt` (real worktree, adapter call, then
   `result_commit`/`changed_paths` derived from git, not from the node).
3. Failure resolution — `_resolve_failed_tasks` drives each failing task
   through a `retry_or_escalate_policy` decision (or straight to
   REQUEST_USER_INPUT when the runner flagged the failure unrecoverable);
   proposals only take effect via `apply_decision`.
4. Gate checks — a required task whose latest attempt is still FAILED after
   resolution fails the run (FAILED_EXHAUSTED); otherwise
   `all_required_gates_met` is re-derived from stored evidence.
5. Completion — the run becomes COMPLETED_VERIFIED only from that check,
   never from a node's or decision's own claim of success.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from forge.controller.attempt_commit import (
    GitStateError,
    changed_paths_between,
    commit_dirty_changes,
    current_head,
)
from forge.controller.decision_dispatch import (
    DEFAULT_FALLBACK_POLICIES,
    DecisionFn,
    FallbackFn,
    invoke_decision_node,
)
from forge.controller.decisions import RejectedProposalError, apply_decision
from forge.controller.execution_registry import ExecutionHandlerRegistry, ExecutionRunner
from forge.controller.mechanical import MechanicalContext, MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeType, NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.store import Store
from forge.isolation.worktree_manager import create_worktree
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.common import NodeKind
from forge.models.decision import DecisionAction, DecisionProposal
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
    decision_fn: DecisionFn | None = None
    fallback_policies: dict[str, FallbackFn] = field(
        default_factory=lambda: dict(DEFAULT_FALLBACK_POLICIES)
    )
    # EXECUTION-kind dispatch is entirely optional: with execution_runner
    # left as None, EXECUTION tasks behave exactly as they did before this
    # existed (never become ready, the run stalls on them) — nothing below
    # requires an execution runtime to be configured.
    execution_handler_registry: ExecutionHandlerRegistry = field(
        default_factory=ExecutionHandlerRegistry
    )
    execution_runner: ExecutionRunner | None = None
    repo_root: Path | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(UTC)


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


def _ready_tasks(
    graph: GraphVersion, attempts: list[Attempt], *, execution_available: bool = False
) -> list[TaskSpec]:
    latest_by_task = _latest_attempt_per_task(attempts)

    ready: list[TaskSpec] = []
    for task in graph.tasks:
        if task.node_kind == NodeKind.EXECUTION and not execution_available:
            # No execution runner is configured for this run — an
            # EXECUTION task simply never becomes ready, stalling the run
            # on it rather than being silently skipped. (With a runner
            # configured, EXECUTION and MECHANICAL tasks follow the exact
            # same readiness rule below.)
            continue
        if task.node_kind not in (NodeKind.MECHANICAL, NodeKind.EXECUTION):
            continue
        current = latest_by_task.get(task.id)
        if current is not None and current.status in (
            AttemptStatus.RUNNING,
            AttemptStatus.PASSED,
            AttemptStatus.FAILED,
        ):
            # RUNNING/PASSED: already in flight or done. FAILED: never
            # silently re-dispatched here — only an explicit decision
            # (handled in _resolve_failed_tasks) can retry it. A CANCELLED
            # or SUPERSEDED latest attempt, by contrast, leaves the task
            # eligible again: CANCELLED means a prior attempt was orphaned
            # by a crash and never really ran; SUPERSEDED means its
            # evidence no longer counts, so the task needs a fresh attempt
            # just as much as if it had never run at all. Checking only the
            # *latest* attempt (not "has any attempt ever had status X")
            # matters here — a task's history can contain an old FAILED
            # attempt followed by a PASSED-then-SUPERSEDED one, and it's
            # the latest status that determines what happens next.
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


def _upstream_worktree_path(task: TaskSpec, attempts: list[Attempt]) -> str | None:
    """
    A mechanical task that depends on something which produced a worktree
    (an EXECUTION attempt) runs inside that same worktree — it's verifying
    that output, not starting fresh. Returns the first upstream dependency's
    worktree path found among PASSED attempts, or None if none of them
    produced one (ordinary mechanical-only chains, unaffected).
    """
    latest_by_task = _latest_attempt_per_task(attempts)
    for dep_id in task.depends_on:
        dep_attempt = latest_by_task.get(dep_id)
        if dep_attempt and dep_attempt.status == AttemptStatus.PASSED and dep_attempt.worktree_path:
            return dep_attempt.worktree_path
    return None


def dispatch_mechanical_attempt(
    task: TaskSpec,
    run: Run,
    attempt_number: int,
    store: Store,
    mechanical_registry: MechanicalRunnerRegistry,
    *,
    model_used: str = "mechanical",
    retries_from_attempt_id: str | None = None,
    escalated_from_attempt_id: str | None = None,
    all_attempts: list[Attempt] | None = None,
) -> Attempt:
    """
    Creates and synchronously runs one attempt for `task`. Used both by the
    normal dependency-driven readiness loop (a fresh task, no retry/escalate
    lineage) and by decision handling (a forward-pointing retry/escalation
    attempt) — the only difference is the optional lineage/model fields.

    Runs inside an upstream EXECUTION dependency's worktree when one exists
    (`all_attempts` is how the caller supplies the attempt history needed to
    find it); otherwise behaves exactly as before.
    """
    worktree_path = _upstream_worktree_path(task, all_attempts or [])
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
        worktree_path=worktree_path or "",
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
    result = runner(task, MechanicalContext(worktree_path=worktree_path))

    attempt.status = AttemptStatus.PASSED if result.passed else AttemptStatus.FAILED
    attempt.finished_at = _now()
    attempt.failure_summary = None if result.passed else result.summary
    attempt.outputs = result.outputs
    attempt.requires_user_input = (not result.passed) and result.requires_user_input

    commit_sha = current_head(worktree_path) if worktree_path else ""
    evidence = [
        Evidence(
            id=new_id("ev"),
            run_id=run.id,
            attempt_id=attempt.id,
            task_id=task.id,
            acceptance_criterion_id=criterion_id,
            kind="test_result",
            passed=result.passed,
            commit_sha=commit_sha,
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


def _collect_upstream_outputs(
    task: TaskSpec, graph: GraphVersion, attempts: list[Attempt], node_registry: NodeTypeRegistry
) -> dict[str, object]:
    """
    Builds the `{upstream_task_id: parsed_output_model}` mapping an
    EXECUTION node's `build_input` consumes. Only PASSED dependencies
    contribute, and only if their recorded output actually parses back
    into their own node type's output schema — a node's context is built
    from what upstream work actually produced, never from guessing.
    """
    task_by_id = {t.id: t for t in graph.tasks}
    latest_by_task = _latest_attempt_per_task(attempts)
    outputs: dict[str, object] = {}
    for dep_id in task.depends_on:
        dep_attempt = latest_by_task.get(dep_id)
        if dep_attempt is None or dep_attempt.status != AttemptStatus.PASSED:
            continue
        dep_task = task_by_id.get(dep_id)
        if dep_task is None:
            continue
        try:
            dep_node_type = node_registry.get(dep_task.node_type)
            outputs[dep_id] = dep_node_type.output_schema.model_validate(dep_attempt.outputs)
        except Exception:
            continue
    return outputs


_RESULT_FILE_NAME = ".forge_result.json"


def _read_result_file(
    worktree_path: str, node_type: NodeType, fallback_summary: str
) -> tuple[bool, str, dict[str, object]]:
    """
    For `output_strategy == "result_file"` node types: reads and validates
    `.forge_result.json` from the worktree rather than trusting anything
    said in the session's own transcript. The node type's own
    `output_schema` is expected to declare a `passed: bool` field for this
    strategy — that's the contract a result-file node type opts into by
    choosing this strategy. Any failure to find, parse, or validate the
    file is treated as the attempt failing, with a summary explaining why,
    never as a silent default pass.
    """
    result_path = Path(worktree_path) / _RESULT_FILE_NAME
    if not result_path.exists():
        return False, f"{_RESULT_FILE_NAME} was not written", {}
    try:
        raw = json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"failed to read {_RESULT_FILE_NAME}: {exc}", {}
    try:
        validated = node_type.output_schema.model_validate(raw)
    except ValidationError as exc:
        return False, f"{_RESULT_FILE_NAME} didn't match the expected shape: {exc}", {}

    output_dict = validated.model_dump()
    passed = bool(output_dict.get("passed", False))
    summary = str(output_dict.get("reasoning") or output_dict.get("summary") or fallback_summary)
    return passed, summary, output_dict


def dispatch_execution_attempt(
    task: TaskSpec,
    run: Run,
    graph: GraphVersion,
    request: Request,
    attempt_number: int,
    store: Store,
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    execution_handler_registry: ExecutionHandlerRegistry,
    execution_runner: ExecutionRunner,
    repo_root: Path,
    *,
    model_used: str | None = None,
    retries_from_attempt_id: str | None = None,
    escalated_from_attempt_id: str | None = None,
) -> Attempt:
    """
    Creates and synchronously runs one EXECUTION attempt: a fresh worktree,
    a prompt built from the node type's own context-building logic, a call
    to whatever runtime `execution_runner` wraps (Claude Agent SDK today),
    and then — regardless of what the runner reports — a mechanical
    derivation of the actual result from git: any uncommitted changes in
    the worktree get committed as a safety net, the resulting HEAD becomes
    `result_commit`, and a real `git diff` against the base commit becomes
    `changed_paths`. A node's own claims about what it did are never the
    source of truth for this; git is.
    """
    node_type = node_registry.get(task.node_type)
    profile = permission_registry.get(task.permission_profile)
    handlers = execution_handler_registry.get(task.node_type)
    model = model_used or node_type.default_model
    if model is None:
        raise ValueError(f"no model configured for node type '{task.node_type}'")

    attempt_id = new_id("att")
    base_commit = current_head(repo_root)
    worktree = create_worktree(repo_root, task.id, attempt_id, base_commit)

    attempt = Attempt(
        id=attempt_id,
        run_id=run.id,
        task_id=task.id,
        graph_version_id=run.active_graph_version_id,
        attempt_number=attempt_number,
        retries_from_attempt_id=retries_from_attempt_id,
        escalated_from_attempt_id=escalated_from_attempt_id,
        model_used=model,
        permission_profile=task.permission_profile,
        worktree_path=worktree.worktree_path,
        branch_name=worktree.branch_name,
        base_commit=base_commit,
        status=AttemptStatus.RUNNING,
        started_at=_now(),
    )
    # Durable before execution: if the process dies right after this write,
    # the attempt is left RUNNING for reconcile_orphaned_attempts to find
    # and cancel on the next startup — never silently lost.
    store.save_attempt(attempt)

    attempts = store.load_attempts(run.id)
    upstream_outputs = _collect_upstream_outputs(task, graph, attempts, node_registry)
    input_data = handlers.build_input(task, request, upstream_outputs)
    prompt = handlers.build_prompt(input_data)

    outcome = execution_runner(model, prompt, profile, worktree.worktree_path)

    passed = False
    summary = outcome.summary
    output_dict: dict[str, object] = {}
    result_commit: str | None = None
    changed_paths: list[str] = []

    if outcome.completed:
        try:
            commit_dirty_changes(worktree.worktree_path, f"forge: {task.id} attempt {attempt.id}")
            result_commit = current_head(worktree.worktree_path)
        except GitStateError as exc:
            result_commit = base_commit
            summary = f"failed to determine resulting commit: {exc}"
        if result_commit == base_commit:
            summary = (
                f"no changes were committed (runner reported: {summary})"
                if summary
                else "no changes were committed"
            )
            result_commit = None
        else:
            changed_paths = changed_paths_between(
                worktree.worktree_path, base_commit, result_commit
            )
            if node_type.output_strategy == "result_file":
                passed, summary, output_dict = _read_result_file(
                    worktree.worktree_path, node_type, summary
                )
            else:
                output_dict = {
                    "result_commit": result_commit,
                    "changed_paths": changed_paths,
                    "summary": summary,
                }
                passed = True

    attempt.status = AttemptStatus.PASSED if passed else AttemptStatus.FAILED
    attempt.finished_at = _now()
    attempt.failure_summary = None if passed else summary
    attempt.outputs = output_dict
    attempt.result_commit = result_commit

    evidence = [
        Evidence(
            id=new_id("ev"),
            run_id=run.id,
            attempt_id=attempt.id,
            task_id=task.id,
            acceptance_criterion_id=criterion_id,
            kind="test_result",
            passed=passed,
            commit_sha=result_commit or base_commit,
            touched_paths=changed_paths,
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
                "summary": summary,
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


def _unrecoverable_failure_proposal(task: TaskSpec, failed_attempt: Attempt) -> DecisionProposal:
    """
    Synthesizes a REQUEST_USER_INPUT proposal directly, with no decision
    adapter or fallback policy involved, for a failure the runner itself
    already identified as unrecoverable by retrying (`Attempt.
    requires_user_input`). This is mechanical, not judgment — the signal
    is unambiguous, so no model call is worth making for it, and routing
    it through `retry_or_escalate_policy` at all would just spend the
    task's retry budget on repeats of a failure no retry could fix.
    """
    return DecisionProposal(
        id=new_id("dec"),
        decision_node_type="unrecoverable_failure_policy",
        action=DecisionAction.REQUEST_USER_INPUT,
        target_task_ids=[task.id],
        user_question=(
            f"Task '{task.id}' failed in a way that retrying cannot fix: "
            f"{failed_attempt.failure_summary}"
        ),
        confidence=1.0,
        evidence_refs=[f"attempt:{failed_attempt.id}"],
        model_used="mechanical:unrecoverable_failure_policy",
        created_at=_now(),
    )


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
        if task.node_kind == NodeKind.EXECUTION:
            if deps.execution_runner is None or deps.repo_root is None:
                raise RejectedProposalError(
                    f"'{task.id}' is an EXECUTION task but no execution runner is configured"
                )
            return dispatch_execution_attempt(
                task,
                run,
                graph,
                request,
                attempt_number,
                store,
                deps.node_registry,
                deps.permission_registry,
                deps.execution_handler_registry,
                deps.execution_runner,
                deps.repo_root,
                model_used=model_used,
                retries_from_attempt_id=retries_from_attempt_id,
                escalated_from_attempt_id=escalated_from_attempt_id,
            )
        return dispatch_mechanical_attempt(
            task,
            run,
            attempt_number,
            store,
            deps.mechanical_registry,
            model_used=model_used,
            retries_from_attempt_id=retries_from_attempt_id,
            escalated_from_attempt_id=escalated_from_attempt_id,
            all_attempts=attempts,
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
            if failed_attempt.requires_user_input:
                proposal = _unrecoverable_failure_proposal(task, failed_attempt)
            else:
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
    execution_available = deps.execution_runner is not None and deps.repo_root is not None

    for task in _ready_tasks(graph, attempts, execution_available=execution_available):
        attempt_number = 1 + sum(1 for a in attempts if a.task_id == task.id)
        if task.node_kind == NodeKind.EXECUTION:
            dispatched = dispatch_execution_attempt(
                task,
                run,
                graph,
                request,
                attempt_number,
                store,
                deps.node_registry,
                deps.permission_registry,
                deps.execution_handler_registry,
                deps.execution_runner,
                deps.repo_root,
            )
        else:
            dispatched = dispatch_mechanical_attempt(
                task, run, attempt_number, store, deps.mechanical_registry, all_attempts=attempts
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


def _can_make_progress(
    graph: GraphVersion, attempts: list[Attempt], *, execution_available: bool = False
) -> bool:
    return bool(_ready_tasks(graph, attempts, execution_available=execution_available)) or bool(
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
    execution_available = deps.execution_runner is not None and deps.repo_root is not None
    for _ in range(max_ticks):
        run = store.load_run(run_id)
        if run.status != RunStatus.ACTIVE:
            return run
        graph = store.load_graph_version(run.active_graph_version_id)
        attempts = store.load_attempts(run_id)
        if not _can_make_progress(graph, attempts, execution_available=execution_available):
            return run
        run = controller_tick(run_id, store, deps)
    raise RuntimeError(f"run '{run_id}' did not reach a terminal state within {max_ticks} ticks")
