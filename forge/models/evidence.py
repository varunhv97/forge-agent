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
    invalidated_by_evidence_id: str | None = None
    created_at: datetime
