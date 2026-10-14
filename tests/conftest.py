from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from forge.controller.node_registry import NodeType, NodeTypeRegistry, PermissionProfileRegistry
from forge.models.common import NodeKind
from forge.models.graph import EdgeSpec, GraphVersion, TaskSpec
from forge.models.permissions import Budgets, PermissionProfile
from forge.models.request import AcceptanceCriterion, Request


class EmptyIO(BaseModel):
    pass


def make_node_type(
    name: str,
    kind: NodeKind = NodeKind.EXECUTION,
    required_upstream_node_types: frozenset[str] = frozenset(),
    produces_gate_evidence: bool = False,
    permission_profile: str = "default",
) -> NodeType:
    return NodeType(
        name=name,
        kind=kind,
        input_schema=EmptyIO,
        output_schema=EmptyIO,
        permission_profile=permission_profile,
        required_upstream_node_types=frozenset(required_upstream_node_types),
        produces_gate_evidence=produces_gate_evidence,
    )


@pytest.fixture
def node_registry() -> NodeTypeRegistry:
    registry = NodeTypeRegistry()
    registry.register(make_node_type("root_exec"))
    registry.register(make_node_type("other_root_exec"))
    registry.register(
        make_node_type(
            "verify_exec",
            kind=NodeKind.MECHANICAL,
            required_upstream_node_types=frozenset({"root_exec"}),
            produces_gate_evidence=True,
        )
    )
    registry.register(
        make_node_type(
            "deploy_exec",
            kind=NodeKind.MECHANICAL,
            permission_profile="network",
            produces_gate_evidence=True,
        )
    )
    return registry


@pytest.fixture
def permission_registry() -> PermissionProfileRegistry:
    registry = PermissionProfileRegistry()
    registry.register(PermissionProfile(name="default", allowed_tools=["Read", "Edit"]))
    registry.register(
        PermissionProfile(name="network", allowed_tools=["Bash"], network_allowed=True)
    )
    return registry


def make_request(
    acceptance_criteria: list[AcceptanceCriterion] | None = None,
    deploy_authorized: bool = False,
) -> Request:
    return Request(
        id="req-1",
        mode="feature_blast",
        objective_text="test objective",
        acceptance_criteria=acceptance_criteria or [],
        deploy_authorized=deploy_authorized,
        budgets=Budgets(max_cost_usd=10.0, max_wall_seconds=3600),
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def request_factory():
    return make_request


def make_task(
    task_id: str,
    node_type: str = "root_exec",
    node_kind: NodeKind = NodeKind.EXECUTION,
    depends_on: list[str] | None = None,
    acceptance_criterion_ids: list[str] | None = None,
    permission_profile: str = "default",
    required: bool = True,
) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        description=f"task {task_id}",
        node_kind=node_kind,
        node_type=node_type,
        depends_on=depends_on or [],
        acceptance_criterion_ids=acceptance_criterion_ids or [],
        permission_profile=permission_profile,
        required=required,
    )


def make_graph(
    tasks: list[TaskSpec],
    edges: list[EdgeSpec] | None = None,
    version: int = 1,
    parent_version: int | None = None,
    created_by: str = "template",
) -> GraphVersion:
    if edges is None:
        # default: one dependency edge per depends_on entry
        edges = [
            EdgeSpec(from_task=dep, to_task=t.id, relation="dependency")
            for t in tasks
            for dep in t.depends_on
        ]
    return GraphVersion(
        id=f"gv-{version}",
        request_id="req-1",
        version=version,
        parent_version=parent_version,
        tasks=tasks,
        edges=edges,
        created_by=created_by,
        created_at=datetime.now(UTC),
    )


@pytest.fixture
def task_factory():
    return make_task


@pytest.fixture
def graph_factory():
    return make_graph
