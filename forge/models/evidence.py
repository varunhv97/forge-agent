from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Finding(BaseModel):
    id: str
    attempt_id: str
    title: str
    severity: Severity
    reproducible: bool
    reproduction_ref: str | None = None
    affected_paths: list[str] = []


class Evidence(BaseModel):
    id: str
    run_id: str
    attempt_id: str
    task_id: str
    acceptance_criterion_id: str | None = None
    kind: Literal[
        "test_result", "journey_check", "deploy_probe", "static_check", "finding_set", "manual_note"
    ]
    passed: bool
    commit_sha: str  # exact revision this evidence concerns
    touched_paths: list[str] = []  # files the check actually covers
    raw_artifact_id: str
    # What invalidated this evidence, if anything: either another
    # evidence row's id (the automatic commit/touched-paths staleness
    # rule), or "decision:<DecisionProposal.id>" when a graph revision
    # explicitly invalidated it. Never both meanings mixed into one id —
    # callers that need to resolve a real Evidence row should check for
    # the "decision:" prefix first.
    invalidated_by_evidence_id: str | None = None
    created_at: datetime
