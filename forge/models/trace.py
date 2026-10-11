from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class TraceEvent(BaseModel):
    """
    One entry in a run's human-readable audit trail: what happened, why,
    and what it cost. `forge trace <run_id>` reads these back in order.
    """

    id: str
    run_id: str
    event_type: str
    payload: dict[str, Any] = {}
    created_at: datetime
