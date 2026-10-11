from __future__ import annotations

from typing import Type

from pydantic import BaseModel

from forge.models.common import NodeKind
from forge.models.permissions import PermissionProfile


class NodeType(BaseModel):
    """
    One entry per node type (plan §6.1) — global and mode-agnostic. A mode's
    template function only ever references these by name; it never defines
    its own.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    kind: NodeKind
    input_schema: Type[BaseModel]
    output_schema: Type[BaseModel]
    permission_profile: str
    # Which upstream node TYPES must appear among a task's depends_on for its
    # input to be satisfiable. This is a deliberately simpler, concrete
    # stand-in for the plan's `input_schema.is_satisfiable_from(...)`
    # pseudocode — real enough to fail closed on a genuinely missing
    # dependency, without inventing a fragile schema-matching heuristic.
    required_upstream_node_types: frozenset[str] = frozenset()
    produces_gate_evidence: bool = False
    default_model: str | None = None
    escalation_model: str | None = None


class UnknownNodeTypeError(KeyError):
    pass


class NodeTypeRegistry:
    """One process-wide instance, populated at startup from forge/nodes/*.py."""

    def __init__(self) -> None:
        self._by_name: dict[str, NodeType] = {}

    def register(self, node_type: NodeType) -> None:
        if node_type.name in self._by_name:
            raise ValueError(f"node type '{node_type.name}' already registered")
        self._by_name[node_type.name] = node_type

    def get(self, name: str) -> NodeType:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise UnknownNodeTypeError(name) from exc

    def __contains__(self, name: str) -> bool:
        return name in self._by_name


class UnknownPermissionProfileError(KeyError):
    pass


class PermissionProfileRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, PermissionProfile] = {}

    def register(self, profile: PermissionProfile) -> None:
        if profile.name in self._by_name:
            raise ValueError(f"permission profile '{profile.name}' already registered")
        self._by_name[profile.name] = profile

    def get(self, name: str) -> PermissionProfile:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise UnknownPermissionProfileError(name) from exc
