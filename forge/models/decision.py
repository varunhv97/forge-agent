from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from forge.models.graph import EdgeSpec, TaskSpec


class DecisionAction(str, Enum):
    SCHEDULE_ELIGIBLE = "schedule_eligible"
    RETRY_WITH_FEEDBACK = "retry_with_feedback"
    ESCALATE_MODEL = "escalate_model"
    PROPOSE_GRAPH_REVISION = "propose_graph_revision"
    REQUEST_USER_INPUT = "request_user_input"
    STOP_SUCCESS = "stop_success"
    STOP_BLOCKED = "stop_blocked"


class GraphRevisionProposal(BaseModel):
    base_graph_version_id: str
    add_tasks: list[TaskSpec] = []
    add_edges: list[EdgeSpec] = []
    remove_task_ids: list[str] = []  # only for not-yet-run, not-passed-required tasks
    rationale: str
    invalidated_evidence_ids: list[str] = []  # must be explicit, never implicit
    # NOTE: no field here can mutate permission_profile, budgets, or acceptance
    # criteria of existing tasks — those capabilities are structurally absent
    # from this schema, not merely policy-denied.


class DecisionProposal(BaseModel):
    id: str
    decision_node_type: str
    action: DecisionAction
    target_task_ids: list[str] = []
    retry_feedback: str | None = None
    escalate_to_model: str | None = None
    graph_revision: GraphRevisionProposal | None = None
    user_question: str | None = None
    rationale: str | None = None  # free-text explanation, used by STOP_SUCCESS/STOP_BLOCKED
    confidence: float
    evidence_refs: list[str] = []  # required, non-empty for anything but SCHEDULE_ELIGIBLE
    model_used: str
    created_at: datetime


class GraphBuildOp(BaseModel):
    op: Literal["add_task", "add_edge"]
    task: TaskSpec | None = None
    edge: EdgeSpec | None = None


class ConstrainedGraphBuild(BaseModel):
    ops: list[GraphBuildOp]
    # task.node_type is constrained to an allowlist passed in for this call —
    # enforced by the caller, not by this schema.
