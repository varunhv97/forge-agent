from __future__ import annotations

from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.validator import compile_and_validate
from forge.modes.feature_blast import build_feature_blast_request_and_graph
from forge.nodes import register_all


def _registries() -> tuple[NodeTypeRegistry, PermissionProfileRegistry]:
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    register_all(node_registry, permission_registry)
    return node_registry, permission_registry


def test_build_feature_blast_request_and_graph_compiles_cleanly():
    node_registry, permission_registry = _registries()

    request, draft = build_feature_blast_request_and_graph(
        objective="add a /health JSON endpoint",
        journey_description="visiting /health returns a 200 JSON response",
    )

    compiled = compile_and_validate(draft, request, node_registry, permission_registry)

    assert compiled.status == "active"
    assert compiled.version == 1
    assert [t.id for t in compiled.tasks] == ["implement", "unit_verify", "deploy", "manual_test"]


def test_build_feature_blast_request_and_graph_chain_shape():
    _, draft = build_feature_blast_request_and_graph(
        objective="add a /health JSON endpoint",
        journey_description="visiting /health returns a 200 JSON response",
    )

    by_id = {t.id: t for t in draft.tasks}
    assert by_id["implement"].depends_on == []
    assert by_id["unit_verify"].depends_on == ["implement"]
    assert by_id["deploy"].depends_on == ["unit_verify"]
    assert by_id["manual_test"].depends_on == ["deploy"]
    assert by_id["manual_test"].description == "visiting /health returns a 200 JSON response"


def test_build_feature_blast_request_and_graph_covers_every_required_criterion():
    request, _ = build_feature_blast_request_and_graph(
        objective="add a /health JSON endpoint",
        journey_description="visiting /health returns a 200 JSON response",
    )

    assert request.deploy_authorized is True
    criterion_ids = {c.id for c in request.acceptance_criteria}
    assert criterion_ids == {"unit_tests_pass", "deployed", "journey_works"}
