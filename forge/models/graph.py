from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from forge.models.common import NodeKind


class TaskSpec(BaseModel):
    id: str
    description: str
    node_kind: NodeKind
    node_type: str  # registry key, e.g. "implement_exec"
    depends_on: list[str] = []
    acceptance_criterion_ids: list[str] = []
    permission_profile: str
    required: bool = True  # required=True tasks gate completion


class EdgeSpec(BaseModel):
    from_task: str
    to_task: str
    relation: Literal["dependency", "context_only"] = "dependency"
    # context_only edges feed artifacts without a scheduling gate — still
    # must be acyclic and recorded, but only block on artifact availability


class GraphVersion(BaseModel):
    id: str
    request_id: str
    version: int
    parent_version: int | None = None
    tasks: list[TaskSpec]
    edges: list[EdgeSpec] = []
    status: Literal["draft", "active", "superseded"] = "draft"
    revision_rationale: str | None = None  # required if parent_version is not None
    created_by: str  # "template" | "decision_node:<id>" | "user"
    created_at: datetime
