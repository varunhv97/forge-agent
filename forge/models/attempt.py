from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class AttemptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"  # evidence invalidated by later changes


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
