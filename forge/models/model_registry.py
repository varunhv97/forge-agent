from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ModelCapability(BaseModel):
    provider: Literal["anthropic"]
    model_id: str
    adapter_kind: Literal["sdk_execution", "api_decision"]
    supports_tools: bool
    context_window: int
    cost_per_mtok_in: float
    cost_per_mtok_out: float
    eligible_roles: list[str]
