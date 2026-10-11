from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class RunStatus(str, Enum):
    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETED_VERIFIED = "completed_verified"
    BLOCKED_AWAITING_INPUT = "blocked_awaiting_input"
    FAILED_EXHAUSTED = "failed_exhausted"
    CANCELLED = "cancelled"


class Run(BaseModel):
    id: str
    request_id: str
    active_graph_version_id: str
    status: RunStatus
    blocker_message: str | None = None
    created_at: datetime
    updated_at: datetime
