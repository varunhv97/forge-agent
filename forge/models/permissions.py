from __future__ import annotations

from pydantic import BaseModel, Field


class PermissionProfile(BaseModel):
    name: str
    allowed_tools: list[str]
    denied_bash_patterns: list[str] = Field(
        default_factory=lambda: ["git push*", "*vercel deploy*", "curl *", "rm -rf *"]
    )
    path_globs_allowed: list[str] = Field(default_factory=list)
    network_allowed: bool = False
    # Container isolation is derived from which execution adapter runs the
    # task, not declared here — a profile doesn't know which runtime will
    # use it.


class Budgets(BaseModel):
    max_cost_usd: float
    max_wall_seconds: int
    max_retries_per_task: int = 3
    max_concurrency: int = 4
    max_decision_revisions_per_graph_version_lineage: int = 10
