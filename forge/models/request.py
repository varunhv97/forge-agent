from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from forge.models.common import Mode
from forge.models.permissions import Budgets


class AcceptanceCriterion(BaseModel):
    id: str
    description: str
    verification_method: Literal[
        "automated_test", "manual_journey", "mechanical_check", "deployed_probe"
    ]
    required: bool = True


class Request(BaseModel):
    id: str
    mode: Mode
    objective_text: str
    stated_assumptions: list[str] = []
    acceptance_criteria: list[AcceptanceCriterion]
    deploy_authorized: bool = False
    budgets: Budgets
    created_at: datetime
    # Set when mode == Mode.CUSTOM: names which custom DAG template (its
    # DagDefinition.name, from forge/modes/custom_dag.py) produced this
    # request. Always None for every other mode.
    template_name: str | None = None
