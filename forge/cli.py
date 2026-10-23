"""
Formatting and lookup logic behind the `forge trace` / `forge inspect` /
`forge list` / `forge run`/`resume` CLI commands, and the one data layer the
Textual TUI (`forge/tui/*`) shares with them.

The `compute_*` functions are the actual source of truth: pure readers over
`Store` that return typed, frozen dataclasses — no string formatting, no
Textual imports, nothing printed. `build_trace_report`/`build_inspect_report`/
`build_list_runs_report`/`build_run_attempts_summary` are thin formatters on
top of them, producing the exact plain-text output `scripts/forge_cli.py`
prints (that's the one place ruff's T20 "no print" is scoped off, via
pyproject.toml's per-file-ignores for scripts/**). The TUI
(`forge/tui/screens.py`) calls the same `compute_*` functions directly
instead of re-deriving status data or string-parsing a report's own text —
one data layer, two renderers.

Nothing here writes to the store, a run, or anything else — these are pure
readers layered on top of `Store`, same as the core invariant the rest of
`forge/controller/*` enforces for nodes: this is a display layer, not
another place state changes happen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
from forge.models.run import Run, RunStatus
from forge.models.trace import TraceEvent


class TaskNotFoundError(LookupError):
    """Raised by `build_inspect_report`/`compute_run_inspection` when
    `task_id` isn't in the run's active graph."""


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


# -- run list (`forge list` / the dashboard) ---------------------------------


@dataclass(frozen=True)
class RunListEntry:
    """
    One row of `forge list`'s table / the dashboard's run table. `mode`
    isn't printed by `build_list_runs_report` today (see its docstring) but
    is exactly what `DashboardScreen` shows in its own MODE column, so it's
    computed here rather than making the TUI re-derive it with a second,
    independent `load_request` call per run.
    """

    id: str
    mode: str
    objective: str
    status: RunStatus
    updated_at: datetime


def compute_run_list(store: Store) -> list[RunListEntry]:
    """
    Every run known to this `.forge/forge.db`, newest-updated first (see
    `Store.list_runs`). A request that's gone missing (shouldn't happen, but
    `Store` doesn't enforce the foreign key) shows a placeholder rather than
    raising — one bad row shouldn't hide every other run.
    """
    entries: list[RunListEntry] = []
    for run in store.list_runs():
        try:
            request = store.load_request(run.request_id)
            objective = request.objective_text
            mode = request.template_name or request.mode.value
        except NotFoundError:
            objective = "(request not found)"
            mode = "-"
        entries.append(
            RunListEntry(
                id=run.id,
                mode=mode,
                objective=objective,
                status=run.status,
                updated_at=run.updated_at,
            )
        )
    return entries


def build_list_runs_report(store: Store) -> str:
    """
    Every run known to this `.forge/forge.db`, newest-updated first, one
    line each: id, status, objective, updated_at (not mode — see
    `RunListEntry`'s docstring for why that field exists anyway).
    """
    entries = compute_run_list(store)
    if not entries:
        return "(no runs)"

    lines = [f"{'RUN ID':<20} {'STATUS':<22} {'UPDATED':<22} OBJECTIVE"]
    for entry in entries:
        lines.append(
            f"{entry.id:<20} {entry.status.value:<22} "
            f"{_fmt_ts(entry.updated_at):<22} {entry.objective}"
        )
    return "\n".join(lines)


# -- run inspection (`forge inspect` / the TUI's status derivation) ---------


@dataclass(frozen=True)
class TaskStatusInfo:
    """A task's displayed status, derived from its latest attempt (if any)
    — the one derivation `build_inspect_report` and the TUI's live task
    table/DAG canvas both need, now computed in exactly one place."""

    task_id: str
    status: str  # an AttemptStatus value, or "not_started"
    latest_attempt: Attempt | None


@dataclass(frozen=True)
class GateStatus:
    """One required acceptance criterion's gate status within a run."""

    criterion_id: str
    description: str
    met: bool
    satisfied_by_task_id: str | None


@dataclass(frozen=True)
class RunInspection:
    """
    A full structural snapshot of one run: the run/graph/request
    themselves, every attempt and evidence row (so a per-task detail view
    can filter in memory instead of re-querying the store), each task's
    derived status, and whether every required gate is met.
    """

    run: Run
    graph: GraphVersion
    request: Request
    attempts: list[Attempt]
    evidence: list[Evidence]
    task_statuses: dict[str, TaskStatusInfo]
    gates_met: bool
    gates: list[GateStatus]


def _compute_gate_statuses(
    graph: GraphVersion, request: Request, evidence: list[Evidence]
) -> list[GateStatus]:
    live_evidence = EvidenceIndex([e for e in evidence if e.invalidated_by_evidence_id is None])
    statuses: list[GateStatus] = []
    for criterion in request.acceptance_criteria:
        if not criterion.required:
            continue
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
        statuses.append(
            GateStatus(
                criterion_id=criterion.id,
                description=criterion.description,
                met=satisfying_task_id is not None,
                satisfied_by_task_id=satisfying_task_id,
            )
        )
    return statuses


def compute_run_inspection(store: Store, run_id: str) -> RunInspection:
    """
    Loads and derives everything a structural snapshot of `run_id` needs:
    its active graph/request, every attempt and evidence row, each task's
    derived status, and the run's overall required-gate status. Raises
    `NotFoundError` for an unknown run — this is the single place that
    status derivation (`_task_status`/`_latest_attempts_by_task`) happens;
    `build_inspect_report` and the TUI's `LiveRunScreen` both call this
    rather than deriving it themselves.
    """
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    graph = store.load_graph_version(run.active_graph_version_id)
    request = store.load_request(run.request_id)
    attempts = store.load_attempts(run_id)
    evidence = store.load_evidence(run_id)

    latest_by_task = _latest_attempts_by_task(attempts)
    task_statuses = {
        task.id: TaskStatusInfo(
            task_id=task.id,
            status=_task_status(task, latest_by_task),
            latest_attempt=latest_by_task.get(task.id),
        )
        for task in graph.tasks
    }
    gates = _compute_gate_statuses(graph, request, evidence)
    gates_met = all_required_gates_met(graph, request, evidence)

    return RunInspection(
        run=run,
        graph=graph,
        request=request,
        attempts=attempts,
        evidence=evidence,
        task_statuses=task_statuses,
        gates_met=gates_met,
        gates=gates,
    )


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
    inspection = compute_run_inspection(store, run_id)  # NotFoundError propagates
    if task_id is None:
        return _build_run_report(inspection)

    task = next((t for t in inspection.graph.tasks if t.id == task_id), None)
    if task is None:
        raise TaskNotFoundError(
            f"task '{task_id}' not found in run '{run_id}'s active graph "
            f"version '{inspection.graph.id}'"
        )
    return _build_task_report(node_registry, inspection, task)


def _build_run_report(inspection: RunInspection) -> str:
    run, graph = inspection.run, inspection.graph

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
        status = inspection.task_statuses[task.id].status
        req_tag = "required" if task.required else "optional"
        lines.append(
            f"  {task.id:<20} [{task.node_kind.value}/{task.node_type}] " f"{status} ({req_tag})"
        )

    lines.append("")
    lines.append(f"Required gates (overall met: {inspection.gates_met}):")
    if not inspection.gates:
        lines.append("  (none)")
    for gate in inspection.gates:
        tag = "MET" if gate.met else "UNMET"
        detail = (
            f"(satisfied by '{gate.satisfied_by_task_id}')" if gate.met else "(not yet satisfied)"
        )
        lines.append(f"  [{tag}] {gate.criterion_id} — {gate.description} {detail}")
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
    node_registry: NodeTypeRegistry,
    inspection: RunInspection,
    task: TaskSpec,
) -> str:
    run, request = inspection.run, inspection.request
    attempts = sorted(
        (a for a in inspection.attempts if a.task_id == task.id),
        key=lambda a: a.attempt_number,
    )
    evidence = EvidenceIndex(inspection.evidence).for_task(task.id)
    status = inspection.task_statuses[task.id].status

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


# -- trace --------------------------------------------------------------------


@dataclass(frozen=True)
class TraceEntry:
    """
    One entry in a run's chronological trace: an attempt's dispatch
    (derived from `Attempt.started_at`, since no separate "dispatched"
    `TraceEvent` is recorded) or a stored `TraceEvent` (a result, a
    decision outcome, a rejected proposal, ...). `order` tie-breaks an
    exact-timestamp collision (dispatch before result). `text` is the full
    rendered detail for this entry (possibly multi-line, e.g. a graph
    revision's invalidated-evidence note) — everything after the
    `[timestamp]` prefix `build_trace_report` prints; `kind`/`task_id` are
    the structured fields a caller can filter/group on without re-parsing
    `text`.
    """

    timestamp: datetime
    order: int
    kind: str  # "dispatch", or the stored TraceEvent's own event_type
    task_id: str | None
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


def compute_trace_entries(store: Store, run_id: str) -> list[TraceEntry]:
    """
    Every entry in `run_id`'s chronological trace, sorted by
    `(timestamp, order)`. Raises `NotFoundError` for an unknown run, same as
    `build_trace_report`. This is the structured list `LiveRunScreen`'s
    trace tail now appends from directly (tracking how many entries it's
    already rendered) instead of diffing `build_trace_report`'s rendered
    text line by line.
    """
    store.load_run(run_id)  # NotFoundError propagates to the caller; result unused here
    attempts = store.load_attempts(run_id)
    events = store.load_trace_events(run_id)
    evidence = store.load_evidence(run_id)

    entries: list[TraceEntry] = []
    for attempt in attempts:
        if attempt.started_at is not None:
            entries.append(
                TraceEntry(
                    timestamp=attempt.started_at,
                    order=0,
                    kind="dispatch",
                    task_id=attempt.task_id,
                    text=(
                        f"DISPATCH  task={attempt.task_id} attempt={attempt.id} "
                        f"model={attempt.model_used} profile={attempt.permission_profile}"
                    ),
                )
            )
    for event in events:
        entries.append(
            TraceEntry(
                timestamp=event.created_at,
                order=1,
                kind=event.event_type,
                task_id=event.payload.get("task_id"),
                text=_format_trace_event(event, evidence),
            )
        )
    entries.sort(key=lambda e: (e.timestamp, e.order))
    return entries


def build_trace_report(store: Store, run_id: str) -> str:
    """
    A chronological, human-readable replay of everything that happened in a
    run: every attempt's dispatch and result, every
    `DecisionProposal`/`apply_decision` outcome, and — for a graph revision
    that invalidated evidence — which tasks that invalidation propagation
    actually reached.
    """
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    entries = compute_trace_entries(store, run_id)

    lines = [f"Trace for run {run.id} (request {run.request_id}, status={run.status.value})", ""]
    if not entries:
        lines.append("(no recorded activity)")
    lines.extend(f"[{_fmt_ts(entry.timestamp)}] {entry.text}" for entry in entries)
    return "\n".join(lines)


# -- run / resume -----------------------------------------------------------


@dataclass(frozen=True)
class RunAttemptsSummary:
    """A run's final status plus every attempt it produced, sorted by
    `(task_id, attempt_number)` — what `forge run`/`forge resume` print
    once `run_to_completion` returns."""

    run: Run
    attempts: list[Attempt] = field(default_factory=list)


def compute_run_attempts_summary(store: Store, run_id: str) -> RunAttemptsSummary:
    run = store.load_run(run_id)  # NotFoundError propagates to the caller
    attempts = sorted(store.load_attempts(run_id), key=lambda a: (a.task_id, a.attempt_number))
    return RunAttemptsSummary(run=run, attempts=attempts)


def build_run_attempts_summary(store: Store, run_id: str) -> str:
    """
    A plain-text summary of one run's final status and every attempt it
    produced, in the same shape `scripts/live_feature_blast_m7.py` already
    prints after `run_to_completion` returns — `forge run`/`forge resume`
    reuse this rather than re-deriving their own formatting.
    """
    summary = compute_run_attempts_summary(store, run_id)
    lines = [f"Final run status: {summary.run.status.value}"]
    for attempt in summary.attempts:
        lines.append(
            f"  {attempt.task_id} attempt#{attempt.attempt_number}: {attempt.status.value} "
            f"(worktree={attempt.worktree_path or '-'})"
        )
        if attempt.failure_summary:
            lines.append(f"    failure: {attempt.failure_summary}")
        if attempt.result_commit:
            lines.append(f"    result_commit: {attempt.result_commit}")
    if summary.run.blocker_message:
        lines.append(f"  blocked on: {summary.run.blocker_message}")
    return "\n".join(lines)
