from __future__ import annotations

import networkx as nx

from forge.controller.evidence_index import EvidenceIndex
from forge.controller.node_registry import (
    NodeTypeRegistry,
    PermissionProfileRegistry,
    UnknownNodeTypeError,
    UnknownPermissionProfileError,
)
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.graph import GraphVersion, TaskSpec
from forge.models.request import Request


class GraphValidationError(Exception):
    """Raised by compile_and_validate. Never caught and silently ignored by
    the controller — a node/decision proposal that triggers this is rejected,
    not patched around."""


def _check_unique_ids(tasks: list[TaskSpec]) -> None:
    seen: set[str] = set()
    for t in tasks:
        if t.id in seen:
            raise GraphValidationError(f"duplicate task id: {t.id}")
        seen.add(t.id)


def _check_edges_reference_existing_tasks(draft: GraphVersion) -> None:
    task_ids = {t.id for t in draft.tasks}
    for e in draft.edges:
        if e.from_task not in task_ids:
            raise GraphValidationError(f"edge references unknown task '{e.from_task}'")
        if e.to_task not in task_ids:
            raise GraphValidationError(f"edge references unknown task '{e.to_task}'")


def _check_depends_on_matches_edges(draft: GraphVersion) -> None:
    dependency_pairs = {(e.from_task, e.to_task) for e in draft.edges if e.relation == "dependency"}
    for t in draft.tasks:
        for dep in t.depends_on:
            if (dep, t.id) not in dependency_pairs:
                raise GraphValidationError(
                    f"{t.id}: depends_on '{dep}' has no matching dependency edge in graph.edges"
                )


def _build_digraph(draft: GraphVersion) -> nx.DiGraph[str]:
    g: nx.DiGraph[str] = nx.DiGraph()
    for t in draft.tasks:
        g.add_node(t.id)
    for e in draft.edges:
        g.add_edge(e.from_task, e.to_task)
    return g


def _check_acyclic(g: nx.DiGraph[str]) -> None:
    if not nx.is_directed_acyclic_graph(g):
        cycle = next(nx.simple_cycles(g))
        raise GraphValidationError(f"cycle introduced: {cycle}")


def _check_node_types_valid(draft: GraphVersion, registry: NodeTypeRegistry) -> None:
    for t in draft.tasks:
        try:
            node_type = registry.get(t.node_type)
        except UnknownNodeTypeError as exc:
            raise GraphValidationError(f"{t.id}: unknown node_type '{t.node_type}'") from exc
        if node_type.kind != t.node_kind:
            raise GraphValidationError(
                f"{t.id}: node_kind={t.node_kind} doesn't match registered "
                f"node_type '{t.node_type}' (kind={node_type.kind})"
            )


def _check_dependency_compatibility(draft: GraphVersion, registry: NodeTypeRegistry) -> None:
    """
    Verifies that a task's required upstream node types are actually present
    among its dependencies. This is a conservative, name-based check rather
    than full schema unification: a task declares which upstream node TYPES
    it needs (via NodeType.required_upstream_node_types), and this fails
    closed if none of its dependencies are of that type.
    """
    task_by_id = {t.id: t for t in draft.tasks}
    for t in draft.tasks:
        node_type = registry.get(t.node_type)
        if not node_type.required_upstream_node_types:
            continue
        upstream_types = {
            registry.get(task_by_id[dep].node_type).name
            for dep in t.depends_on
            if dep in task_by_id
        }
        missing = node_type.required_upstream_node_types - upstream_types
        if missing:
            raise GraphValidationError(
                f"{t.id}: missing required upstream node type(s) {sorted(missing)}"
            )


def _check_all_required_criteria_covered(
    draft: GraphVersion, request: Request, registry: NodeTypeRegistry
) -> None:
    for criterion in request.acceptance_criteria:
        if not criterion.required:
            continue
        covered = False
        for t in draft.tasks:
            if not t.required or criterion.id not in t.acceptance_criterion_ids:
                continue
            if registry.get(t.node_type).produces_gate_evidence:
                covered = True
                break
        if not covered:
            raise GraphValidationError(
                f"required acceptance criterion '{criterion.id}' has no covering task "
                f"that produces gate evidence"
            )


def _diff_touched_task_ids(prior: GraphVersion, draft: GraphVersion) -> set[str]:
    prior_by_id = {t.id: t for t in prior.tasks}
    draft_by_id = {t.id: t for t in draft.tasks}
    touched: set[str] = set()
    for tid, prior_task in prior_by_id.items():
        draft_task = draft_by_id.get(tid)
        if draft_task is None or draft_task != prior_task:
            touched.add(tid)  # removed, or altered
    return touched


def _check_no_silent_invalidation(
    draft: GraphVersion,
    prior_version: GraphVersion | None,
    completed_attempts: list[Attempt],
    evidence_index: EvidenceIndex,
    invalidated_evidence_ids: frozenset[str],
) -> None:
    if prior_version is None:
        return
    touched = _diff_touched_task_ids(prior_version, draft)
    if not touched:
        return
    attempts_by_task: dict[str, list[Attempt]] = {}
    for a in completed_attempts:
        attempts_by_task.setdefault(a.task_id, []).append(a)
    for task_id in touched:
        if not any(a.status == AttemptStatus.PASSED for a in attempts_by_task.get(task_id, [])):
            continue
        uninvalidated = [
            e for e in evidence_index.for_task(task_id) if e.id not in invalidated_evidence_ids
        ]
        if uninvalidated:
            raise GraphValidationError(
                f"'{task_id}' has PASSED evidence but this revision alters/removes it "
                f"without explicit invalidation (evidence ids: "
                f"{[e.id for e in uninvalidated]})"
            )


def _check_permission_ceilings(
    draft: GraphVersion, request: Request, permission_registry: PermissionProfileRegistry
) -> None:
    for t in draft.tasks:
        try:
            profile = permission_registry.get(t.permission_profile)
        except UnknownPermissionProfileError as exc:
            raise GraphValidationError(
                f"{t.id}: unknown permission profile '{t.permission_profile}'"
            ) from exc
        if profile.network_allowed and not request.deploy_authorized:
            raise GraphValidationError(
                f"{t.id}: permission profile '{profile.name}' allows network access "
                f"but request.deploy_authorized=False"
            )


def compile_and_validate(
    draft: GraphVersion,
    request: Request,
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    prior_version: GraphVersion | None = None,
    completed_attempts: list[Attempt] | None = None,
    evidence_index: EvidenceIndex | None = None,
    invalidated_evidence_ids: frozenset[str] = frozenset(),
) -> GraphVersion:
    """
    The single enforcement point for graph structure. Every caller — a mode
    template building the initial graph, or a decision node's proposed
    revision — goes through this exact function. There is no looser
    validation path for generated graphs; a revision that fails any of
    these checks is rejected outright, regardless of its source.

    Raises GraphValidationError on the first violated invariant:
      - structural soundness (unique ids, valid edges, acyclicity)
      - every task's node_type is registered and kind-compatible
      - every task's required upstream node types are actually dependencies
      - every required acceptance criterion is covered by a gate-producing task
      - no revision silently invalidates already-PASSED required work
      - every task's permission profile respects the request's ceilings
    """
    completed_attempts = completed_attempts or []
    evidence_index = evidence_index if evidence_index is not None else EvidenceIndex()

    _check_unique_ids(draft.tasks)
    _check_edges_reference_existing_tasks(draft)
    _check_depends_on_matches_edges(draft)
    g = _build_digraph(draft)
    _check_acyclic(g)

    _check_node_types_valid(draft, node_registry)
    _check_dependency_compatibility(draft, node_registry)
    _check_all_required_criteria_covered(draft, request, node_registry)
    _check_no_silent_invalidation(
        draft, prior_version, completed_attempts, evidence_index, invalidated_evidence_ids
    )
    _check_permission_ceilings(draft, request, permission_registry)

    compiled = draft.model_copy(
        update={
            "version": (prior_version.version + 1) if prior_version else 1,
            "status": "active",
        }
    )
    return compiled


def propagate_invalidation(
    draft: GraphVersion, evidence_index: EvidenceIndex, invalidated_evidence_ids: frozenset[str]
) -> set[str]:
    """
    Given explicitly invalidated evidence ids, return every downstream task
    id whose dependency chain consumed that evidence — these need
    re-verification, not silent trust that upstream work still holds. This
    is a pure derivation; the caller is responsible for actually marking
    those tasks' attempts superseded.
    """
    if not invalidated_evidence_ids:
        return set()
    seed_task_ids: set[str] = set()
    for eid in invalidated_evidence_ids:
        evidence = evidence_index.get(eid)
        if evidence is not None:
            seed_task_ids.add(evidence.task_id)
    g = _build_digraph(draft)
    affected: set[str] = set(seed_task_ids)
    for tid in seed_task_ids:
        if tid in g:
            affected |= nx.descendants(g, tid)
    return affected
