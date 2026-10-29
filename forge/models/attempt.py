from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from forge.models.common import EffortLevel


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"  # evidence invalidated by later changes
    # A live EXECUTION attempt was interrupted because a person asked to
    # change its model/effort in place (forge/controller/decisions.py::
    # request_execution_swap -> the effort-swap path) — deliberately not
    # FAILED: the session itself never errored, it was cut short on
    # purpose, and _prior_failures/_has_unrecoverable_failure (both of
    # which only ever count AttemptStatus.FAILED) must never see this as
    # a failure. Not PASSED either, since no evidence was produced. Like
    # CANCELLED/SUPERSEDED, a task whose *latest* attempt has this status
    # is still eligible for a fresh dispatch (_ready_tasks), which is how
    # the scheduler picks the task back up — see pending_resume_model/
    # pending_resume_effort below for what that next dispatch carries
    # forward, and scheduler.py::dispatch_execution_attempt for how.
    INTERRUPTED_FOR_SWAP = "interrupted_for_swap"


class Attempt(BaseModel):
    id: str
    run_id: str
    task_id: str
    graph_version_id: str
    attempt_number: int
    retries_from_attempt_id: str | None = None  # forward pointer, never backward
    escalated_from_attempt_id: str | None = None
    model_used: str
    permission_profile: str
    worktree_path: str
    branch_name: str
    base_commit: str
    result_commit: str | None = None
    status: AttemptStatus
    inputs: dict[str, Any] = {}
    outputs: dict[str, Any] = {}
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_summary: str | None = None
    # Set by a mechanical runner that can tell its own failure is not
    # something retrying will fix (e.g. an invalid credential) — the
    # scheduler routes a FAILED attempt with this set straight to
    # REQUEST_USER_INPUT instead of the normal retry_or_escalate_policy
    # decision, so a bad token doesn't burn the task's retry budget on
    # three identical, guaranteed-to-fail attempts.
    requires_user_input: bool = False
    # The real Claude Agent SDK session id (a UUID off the terminal
    # ResultMessage of a live session) for an EXECUTION attempt dispatched
    # through the SDK adapter — set by sdk_node_runner.run_sdk_node via
    # ExecutionRunOutcome.session_id, threaded onto the Attempt by
    # scheduler.dispatch_execution_attempt. Always None for a MECHANICAL
    # attempt and for a Codex CLI attempt (out of scope for this field).
    # Forge only captures and exposes this (via `forge inspect`) for a
    # person to run `claude --resume <sdk_session_id>` themselves — it is
    # never read back by Forge itself, and does not affect retry/resume
    # policy.
    sdk_session_id: str | None = None
    # Set only on an INTERRUPTED_FOR_SWAP attempt: the model/effort a
    # person asked for when they triggered the interrupt (see
    # request_execution_swap), carried forward so this task's *next*
    # dispatch can honor it — scheduler.dispatch_execution_attempt reads
    # these off the latest attempt (plus sdk_session_id above, reused as
    # the session to resume from) rather than mutating the versioned
    # GraphVersion/TaskSpec a custom DAG authored; an Attempt is the
    # project's own append-only history row for exactly this attempt, so
    # this is ephemeral, one-dispatch scheduler state, not graph-authored
    # data. None for every other attempt, including the one dispatched to
    # actually honor them (which is a normal PASSED/FAILED attempt once it
    # runs).
    pending_resume_model: str | None = None
    pending_resume_effort: EffortLevel | None = None
