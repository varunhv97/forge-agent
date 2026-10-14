from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge.models.common import NodeKind
from forge.models.permissions import PermissionProfile


class NodeType(BaseModel):
    """
    One entry per node type, global and mode-agnostic. A mode's template
    function only ever references these by name; it never defines its own.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    kind: NodeKind
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    permission_profile: str
    # Which upstream node TYPES must appear among a task's depends_on for its
    # input to be satisfiable. A conservative, name-based check rather than
    # full schema unification — it fails closed on a genuinely missing
    # dependency without requiring a fragile field-matching heuristic.
    required_upstream_node_types: frozenset[str] = frozenset()
    produces_gate_evidence: bool = False
    default_model: str | None = None
    escalation_model: str | None = None
    # How an EXECUTION node's structured output gets derived after its
    # session ends. "commit_diff" (the default): the dispatcher builds
    # {result_commit, changed_paths, summary} mechanically from git — for
    # nodes whose job is literally to change code (implement_exec,
    # integration_test_write_exec). "result_file": the node writes its own
    # verdict to a fixed file in its worktree (.forge_result.json), which
    # the dispatcher reads and validates against output_schema — for a
    # judgment node like manual_test_exec, where there's no git diff that
    # could mean "passed" or "failed," only the node's own first-person
    # report of what it observed. Either way the dispatcher derives the
    # output from something concrete left behind in the worktree, never
    # from parsing free text out of the session's own transcript.
    output_strategy: Literal["commit_diff", "result_file"] = "commit_diff"


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
