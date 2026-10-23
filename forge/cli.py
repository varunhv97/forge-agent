"""
Formatting and lookup logic behind the `forge trace` / `forge inspect` CLI
commands. Every function here returns a string (or raises) rather than
printing anything itself — the one place that actually prints is
`scripts/forge_cli.py`, which is where ruff's T20 (no `print`) is already
scoped off project-wide, mirroring how `scripts/live_feature_blast_m7.py`
keeps its own logic in `forge/` and stays a thin script itself.

Nothing here writes to the store, a run, or anything else — these are pure
readers layered on top of `Store`, same as the core invariant the rest of
`forge/controller/*` enforces for nodes: this is a display layer, not
another place state changes happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from forge.controller.evidence_index import EvidenceIndex
from forge.controller.node_registry import NodeTypeRegistry, UnknownNodeTypeError
from forge.controller.scheduler import all_required_gates_met
from forge.controller.store import NotFoundError, Store
from forge.models.attempt import Attempt
from forge.models.evidence import Evidence
from forge.models.graph import GraphVersion, TaskSpec
from forge.models.request import Request
from forge.models.run import Run
from forge.models.trace import TraceEvent


class TaskNotFoundError(LookupError):
    """Raised by `build_inspect_report` when `task_id` isn't in the run's active graph."""


def _latest_attempts_by_task(attempts: list[Attempt]) -> dict[str, Attempt]:
    latest: dict[str, Attempt] = {}
    for attempt in attempts:
        current = latest.get(attempt.task_id)
        if current is None or attempt.attempt_number > current.attempt_number:
            latest[attempt.task_id] = attempt
    return latest


def _task_status(task: TaskSpec, latest_by_task: dict[str, Attempt]) -> str:
    attempt = latest_by_task.get(task.id)
    return attempt.status.value if attempt is not None else "not_started"


def _fmt_ts(dt: datetime | None) -> str:
    return dt.isoformat(timespec="seconds") if dt is not None else "-"


# -- trace ------------------------------------------------------------------


@dataclass
class _TimelineEntry:
    timestamp: datetime
    order: int  # tie-break for an exact-timestamp collision: dispatch (0) before result (1)
    text: str


def _format_trace_event(event: TraceEvent, evidence: list[Evidence]) -> str:
    payload = event.payload
    if event.event_type == "attempt_finished":
        return (
            f"RESULT    task={payload.get('task_id')} attempt={payload.get('attempt_id')} "
            f"status={payload.get('status')} - {payload.get('summary') or '(no summary)'}"
        )
    if event.event_type == "proposal_rejected":
        return f"REJECTED  task={payload.get('task_id')} action={payload.get('action')}"
    if event.event_type == "decision_applied":
        line = (
            f"DECISION  {payload.get('decision_node_type')} action={payload.get('action')} "
            f"target={payload.get('target_task_ids')} model={payload.get('model_used')} "
            f"-> {payload.get('outcome')}"
        )
        if payload.get("outcome") == "revised":
            # decisions.py records this event with id=f"tev-{proposal.id}" and marks
            # whatever evidence the revision invalidated with
            # invalidated_by_evidence_id=f"decision:{proposal.id}" — recovering the
            # proposal id from the trace event's own id lets us show which tasks a
            # PROPOSE_GRAPH_REVISION's invalidation propagation actually reached,
            # without the controller needing to record that separately.
            decision_id = event.id.removeprefix("tev-")
            marker = f"decision:{decision_id}"
            affected = sorted(
                {e.task_id for e in evidence if e.invalidated_by_evidence_id == marker}
            )
            if affected:
                line += f"\n            invalidated evidence for tasks: {', '.join(affected)}"
        return line
    return f"{event.event_type.upper()}  {payload}"


def build_trace_report(store: Store, run_id: str) -> str:
    """
    A chronological, human-readable replay of everything that happened in a
    run: every attempt's dispatch (derived from `Attempt.started_at`, since
    no separate "dispatched" `TraceEvent` is recorded) and result, every
    `DecisionProposal`/`apply_decision` outcome, and — for a graph revision
    that invalidated evidence — which tasks that invalidation propagation
    actually reached.
    """
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    attempts = store.load_attempts(run_id)
    events = store.load_trace_events(run_id)
    evidence = store.load_evidence(run_id)

    entries: list[_TimelineEntry] = []
    for attempt in attempts:
        if attempt.started_at is not None:
            entries.append(
                _TimelineEntry(
                    attempt.started_at,
                    0,
                    f"DISPATCH  task={attempt.task_id} attempt={attempt.id} "
                    f"model={attempt.model_used} profile={attempt.permission_profile}",
                )
            )
    for event in events:
        entries.append(_TimelineEntry(event.created_at, 1, _format_trace_event(event, evidence)))
    entries.sort(key=lambda e: (e.timestamp, e.order))

    lines = [f"Trace for run {run.id} (request {run.request_id}, status={run.status.value})", ""]
    if not entries:
        lines.append("(no recorded activity)")
    lines.extend(f"[{_fmt_ts(entry.timestamp)}] {entry.text}" for entry in entries)
    return "\n".join(lines)


# -- inspect ------------------------------------------------------------------


def build_inspect_report(
    store: Store, node_registry: NodeTypeRegistry, run_id: str, task_id: str | None = None
) -> str:
    """
    `run_id` alone gives a structural snapshot of the run itself; a
    `task_id` on top of it (read within that run's active graph version —
    task ids are only unique per graph, not globally, so a run must already
    be in scope to resolve one) switches to a snapshot of that one task.
    Raises `NotFoundError` for an unknown run, `TaskNotFoundError` for a
    `task_id` absent from the run's active graph.
    """
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    graph = store.load_graph_version(run.active_graph_version_id)
    if task_id is None:
        return _build_run_report(store, run, graph)

    task = next((t for t in graph.tasks if t.id == task_id), None)
    if task is None:
        raise TaskNotFoundError(
            f"task '{task_id}' not found in run '{run_id}'s active graph " f"version '{graph.id}'"
        )
    request = store.load_request(run.request_id)
    return _build_task_report(store, node_registry, run, request, task)


def _build_run_report(store: Store, run: Run, graph: GraphVersion) -> str:
    request = store.load_request(run.request_id)
    attempts = store.load_attempts(run.id)
    evidence = store.load_evidence(run.id)
    latest_by_task = _latest_attempts_by_task(attempts)

    lines = [
        f"Run {run.id} — status={run.status.value}",
        f"  request:      {run.request_id}",
        f"  active graph: {graph.id} (version {graph.version}, status={graph.status})",
        f"  created:      {_fmt_ts(run.created_at)}",
        f"  updated:      {_fmt_ts(run.updated_at)}",
    ]
    if run.blocker_message:
        lines.append(f"  blocked on:   {run.blocker_message}")

    lines.append("")
    lines.append(f"Tasks ({len(graph.tasks)}):")
    for task in graph.tasks:
        status = _task_status(task, latest_by_task)
        req_tag = "required" if task.required else "optional"
        lines.append(
            f"  {task.id:<20} [{task.node_kind.value}/{task.node_type}] " f"{status} ({req_tag})"
        )

    lines.append("")
    gates_met = all_required_gates_met(graph, request, evidence)
    lines.append(f"Required gates (overall met: {gates_met}):")
    live_evidence = EvidenceIndex([e for e in evidence if e.invalidated_by_evidence_id is None])
    required_criteria = [c for c in request.acceptance_criteria if c.required]
    if not required_criteria:
        lines.append("  (none)")
    for criterion in required_criteria:
        satisfying_task_id = None
        for task in graph.tasks:
            if not task.required or criterion.id not in task.acceptance_criterion_ids:
                continue
            if any(
                e.acceptance_criterion_id == criterion.id and e.passed
                for e in live_evidence.for_task(task.id)
            ):
                satisfying_task_id = task.id
                break
        tag = "MET" if satisfying_task_id else "UNMET"
        detail = (
            f"(satisfied by '{satisfying_task_id}')"
            if satisfying_task_id
            else "(not yet satisfied)"
        )
        lines.append(f"  [{tag}] {criterion.id} — {criterion.description} {detail}")
    return "\n".join(lines)


def _attempt_lines(attempt: Attempt, output_strategy: str | None) -> list[str]:
    lines = [
        f"  #{attempt.attempt_number} {attempt.id} status={attempt.status.value} "
        f"model={attempt.model_used} started={_fmt_ts(attempt.started_at)} "
        f"finished={_fmt_ts(attempt.finished_at)}"
    ]
    if output_strategy == "result_file":
        passed = attempt.outputs.get("passed")
        reasoning = attempt.outputs.get("reasoning")
        if passed is not None or reasoning is not None:
            lines.append(f"      result_file: passed={passed} reasoning={reasoning!r}")
    elif output_strategy == "commit_diff":
        if attempt.result_commit:
            changed_paths = attempt.outputs.get("changed_paths") or []
            lines.append(
                f"      result_commit={attempt.result_commit} changed_paths={changed_paths}"
            )
    elif attempt.outputs:
        lines.append(f"      outputs: {attempt.outputs}")
    if attempt.failure_summary:
        lines.append(f"      failure: {attempt.failure_summary}")
    if attempt.requires_user_input:
        lines.append("      requires_user_input: True")
    return lines


def _evidence_lines(evidence: list[Evidence]) -> list[str]:
    lines: list[str] = []
    for e in sorted(evidence, key=lambda e: e.created_at):
        status_tag = "invalidated" if e.invalidated_by_evidence_id else "live"
        lines.append(
            f"  {e.id} kind={e.kind} passed={e.passed} criterion={e.acceptance_criterion_id} "
            f"commit={e.commit_sha or '-'} [{status_tag}]"
        )
        if e.invalidated_by_evidence_id:
            lines.append(f"      invalidated_by={e.invalidated_by_evidence_id}")
    return lines


def _build_task_report(
    store: Store,
    node_registry: NodeTypeRegistry,
    run: Run,
    request: Request,
    task: TaskSpec,
) -> str:
    attempts = sorted(
        (a for a in store.load_attempts(run.id) if a.task_id == task.id),
        key=lambda a: a.attempt_number,
    )
    evidence = EvidenceIndex(store.load_evidence(run.id)).for_task(task.id)
    status = _task_status(task, _latest_attempts_by_task(attempts))

    output_strategy: Literal["commit_diff", "result_file"] | None
    try:
        output_strategy = node_registry.get(task.node_type).output_strategy
    except UnknownNodeTypeError:
        output_strategy = None

    lines = [
        f"Task {task.id} (run {run.id})",
        f"  node type:   {task.node_type} ({task.node_kind.value})"
        + (
            f", output_strategy={output_strategy}"
            if output_strategy is not None
            else " — node type not registered"
        ),
        f"  status:      {status}",
        f"  required:    {task.required}",
        f"  description: {task.description}",
        "",
        f"Depends on ({len(task.depends_on)}):",
    ]
    if task.depends_on:
        lines.extend(f"  {dep}" for dep in task.depends_on)
    else:
        lines.append("  (none)")

    criteria_by_id = {c.id: c for c in request.acceptance_criteria}
    lines.append("")
    lines.append(f"Satisfies acceptance criteria ({len(task.acceptance_criterion_ids)}):")
    if task.acceptance_criterion_ids:
        for criterion_id in task.acceptance_criterion_ids:
            criterion = criteria_by_id.get(criterion_id)
            desc = criterion.description if criterion else "(unknown criterion)"
            lines.append(f"  {criterion_id} — {desc}")
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append(f"Attempts ({len(attempts)}):")
    if not attempts:
        lines.append("  (none)")
    for attempt in attempts:
        lines.extend(_attempt_lines(attempt, output_strategy))

    lines.append("")
    lines.append(f"Evidence ({len(evidence)}):")
    if not evidence:
        lines.append("  (none)")
    lines.extend(_evidence_lines(evidence))

    return "\n".join(lines)


# -- list -----------------------------------------------------------------


def build_list_runs_report(store: Store) -> str:
    """
    Every run known to this `.forge/forge.db`, newest-updated first (see
    `Store.list_runs`), one line each: id, status, objective, updated_at.
    The objective comes from the run's `Request` — a request that's gone
    missing (shouldn't happen, but `Store` doesn't enforce the foreign
    key) is shown rather than raised, since one bad row shouldn't hide
    every other run from `forge list`.
    """
    runs = store.list_runs()
    if not runs:
        return "(no runs)"

    lines = [f"{'RUN ID':<20} {'STATUS':<22} {'UPDATED':<22} OBJECTIVE"]
    for run in runs:
        try:
            objective = store.load_request(run.request_id).objective_text
        except NotFoundError:
            objective = "(request not found)"
        lines.append(
            f"{run.id:<20} {run.status.value:<22} {_fmt_ts(run.updated_at):<22} {objective}"
        )
    return "\n".join(lines)


# -- run / resume -----------------------------------------------------------


def build_run_attempts_summary(store: Store, run_id: str) -> str:
    """
    A plain-text summary of one run's final status and every attempt it
    produced, in the same shape `scripts/live_feature_blast_m7.py` already
    prints after `run_to_completion` returns — `forge run`/`forge resume`
    reuse this rather than re-deriving their own formatting.
    """
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    lines = [f"Final run status: {run.status.value}"]
    attempts = sorted(store.load_attempts(run_id), key=lambda a: (a.task_id, a.attempt_number))
    for attempt in attempts:
        lines.append(
            f"  {attempt.task_id} attempt#{attempt.attempt_number}: {attempt.status.value} "
            f"(worktree={attempt.worktree_path or '-'})"
        )
        if attempt.failure_summary:
            lines.append(f"    failure: {attempt.failure_summary}")
        if attempt.result_commit:
            lines.append(f"    result_commit: {attempt.result_commit}")
    if run.blocker_message:
        lines.append(f"  blocked on: {run.blocker_message}")
    return "\n".join(lines)
