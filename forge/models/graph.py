from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from forge.models.common import EffortLevel, NodeKind


class TaskSpec(BaseModel):
    id: str
    description: str
    node_kind: NodeKind
    node_type: str  # registry key, e.g. "implement_exec"
    depends_on: list[str] = []
    acceptance_criterion_ids: list[str] = []
    permission_profile: str
    required: bool = True  # required=True tasks gate completion
    # Optional per-task overrides a custom DAG's JSON can declare (see
    # forge/modes/custom_dag.py's DagTaskSpec.model/effort) — None means
    # "no override," the exact pre-existing behavior. model_override, when
    # set, is validated at custom-DAG-build time against the real
    # ModelRegistry's eligibility check (CustomDagError on a bad value,
    # never trusted raw by the scheduler); dispatch_execution_attempt reads
    # it as `model_used or model_override or node_type.default_model` — it
    # only ever wins over the node type's own default, never over a
    # decision-driven retry/escalation's explicit model_used. effort_override
    # replaces only the node type's own default_effort rung for
    # select_effort's escalation policy; escalation_effort still applies
    # after repeated failures — see scheduler.dispatch_execution_attempt and
    # architecture.md for the reasoning behind not disabling escalation.
    model_override: str | None = None
    effort_override: EffortLevel | None = None


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
