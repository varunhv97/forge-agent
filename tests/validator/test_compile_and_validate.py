from __future__ import annotations

import pytest

from forge.controller.evidence_index import EvidenceIndex
from forge.controller.validator import GraphValidationError, compile_and_validate
from forge.models.attempt import Attempt, AttemptStatus
from forge.models.evidence import Evidence
from forge.models.graph import EdgeSpec
from forge.models.request import AcceptanceCriterion


def _passed_attempt(task_id: str, attempt_id: str = "a1") -> Attempt:
    return Attempt(
        id=attempt_id,
        run_id="run-1",
        task_id=task_id,
        graph_version_id="gv-1",
        attempt_number=1,
        model_used="test-model",
        permission_profile="default",
        worktree_path="/tmp/x",
        branch_name="b",
        base_commit="c0",
        result_commit="c1",
        status=AttemptStatus.PASSED,
    )


def _evidence(task_id: str, evidence_id: str = "e1") -> Evidence:
    return Evidence(
        id=evidence_id,
        run_id="run-1",
        attempt_id="a1",
        task_id=task_id,
        kind="test_result",
        passed=True,
        commit_sha="c1",
        raw_artifact_id="art-1",
        created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )


# --- Structural soundness ------------------------------------------------


def test_minimal_valid_graph_compiles(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1")]
    graph = graph_factory(tasks)
    req = request_factory()
    compiled = compile_and_validate(graph, req, node_registry, permission_registry)
    assert compiled.status == "active"
    assert compiled.version == 1


def test_duplicate_task_ids_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1"), task_factory("t1")]
    graph = graph_factory(tasks)
    with pytest.raises(GraphValidationError, match="duplicate task id"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_edge_referencing_unknown_task_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1")]
    graph = graph_factory(tasks, edges=[EdgeSpec(from_task="ghost", to_task="t1")])
    with pytest.raises(GraphValidationError, match="unknown task"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_depends_on_without_matching_edge_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1"), task_factory("t2", depends_on=["t1"])]
    # deliberately omit the dependency edge that should mirror t2.depends_on
    graph = graph_factory(tasks, edges=[])
    with pytest.raises(GraphValidationError, match="no matching dependency edge"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_cycle_is_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    """A revision cannot introduce a cycle, however indirectly."""
    t1 = task_factory("t1", depends_on=["t2"])
    t2 = task_factory("t2", depends_on=["t1"])
    graph = graph_factory([t1, t2])
    with pytest.raises(GraphValidationError, match="cycle"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_self_dependency_cycle_is_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    t1 = task_factory("t1", depends_on=["t1"])
    graph = graph_factory([t1])
    with pytest.raises(GraphValidationError, match="cycle"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


# --- Node type validity ---------------------------------------------------


def test_unknown_node_type_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1", node_type="does_not_exist")]
    graph = graph_factory(tasks)
    with pytest.raises(GraphValidationError, match="unknown node_type"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_node_kind_mismatch_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    # "root_exec" is registered as EXECUTION; claim it's MECHANICAL instead
    tasks = [task_factory("t1", node_type="root_exec", node_kind=NodeKind.MECHANICAL)]
    graph = graph_factory(tasks)
    with pytest.raises(GraphValidationError, match="kind"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


# --- Dependency/input compatibility ---------------------------------------


def test_missing_required_upstream_node_type_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    # verify_exec requires an upstream "root_exec" — give it an unrelated
    # upstream type instead
    other = task_factory("other", node_type="other_root_exec")
    verify = task_factory(
        "verify", node_type="verify_exec", node_kind=NodeKind.MECHANICAL, depends_on=["other"]
    )
    graph = graph_factory([other, verify])
    with pytest.raises(GraphValidationError, match="missing required upstream node type"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)


def test_satisfied_required_upstream_node_type_compiles(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    root = task_factory("root", node_type="root_exec")
    verify = task_factory(
        "verify", node_type="verify_exec", node_kind=NodeKind.MECHANICAL, depends_on=["root"]
    )
    graph = graph_factory([root, verify])
    compiled = compile_and_validate(graph, request_factory(), node_registry, permission_registry)
    assert compiled.status == "active"


# --- Required completion gates ---------------------------------------------


def test_required_criterion_with_no_covering_task_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    criterion = AcceptanceCriterion(id="c1", description="x", verification_method="automated_test")
    req = request_factory(acceptance_criteria=[criterion])
    tasks = [task_factory("t1")]  # doesn't cover c1, and root_exec doesn't produce gate evidence
    graph = graph_factory(tasks)
    with pytest.raises(GraphValidationError, match="required acceptance criterion"):
        compile_and_validate(graph, req, node_registry, permission_registry)


def test_required_criterion_covered_by_gate_producing_task_compiles(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    criterion = AcceptanceCriterion(id="c1", description="x", verification_method="automated_test")
    req = request_factory(acceptance_criteria=[criterion])
    root = task_factory("root", node_type="root_exec")
    verify = task_factory(
        "verify",
        node_type="verify_exec",
        node_kind=NodeKind.MECHANICAL,
        depends_on=["root"],
        acceptance_criterion_ids=["c1"],
    )
    graph = graph_factory([root, verify])
    compiled = compile_and_validate(graph, req, node_registry, permission_registry)
    assert compiled.status == "active"


def test_optional_criterion_does_not_require_coverage(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    criterion = AcceptanceCriterion(
        id="c1", description="x", verification_method="automated_test", required=False
    )
    req = request_factory(acceptance_criteria=[criterion])
    graph = graph_factory([task_factory("t1")])
    compiled = compile_and_validate(graph, req, node_registry, permission_registry)
    assert compiled.status == "active"


# --- No retroactive invalidation of completed required work -------------


def test_revision_altering_passed_task_without_invalidation_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    prior_task = task_factory("t1", acceptance_criterion_ids=["c1"])
    prior = graph_factory([prior_task], version=1)

    altered_task = task_factory("t1", acceptance_criterion_ids=["c1", "c2"])  # semantics changed
    draft = graph_factory([altered_task], parent_version=1)

    attempts = [_passed_attempt("t1")]
    evidence_index = EvidenceIndex([_evidence("t1")])

    with pytest.raises(GraphValidationError, match="without explicit invalidation"):
        compile_and_validate(
            draft,
            request_factory(),
            node_registry,
            permission_registry,
            prior_version=prior,
            completed_attempts=attempts,
            evidence_index=evidence_index,
        )


def test_revision_altering_passed_task_with_explicit_invalidation_compiles(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    prior_task = task_factory("t1", acceptance_criterion_ids=["c1"])
    prior = graph_factory([prior_task], version=1)

    altered_task = task_factory("t1", acceptance_criterion_ids=["c1", "c2"])
    draft = graph_factory([altered_task], parent_version=1)

    attempts = [_passed_attempt("t1")]
    evidence_index = EvidenceIndex([_evidence("t1", evidence_id="e1")])

    compiled = compile_and_validate(
        draft,
        request_factory(),
        node_registry,
        permission_registry,
        prior_version=prior,
        completed_attempts=attempts,
        evidence_index=evidence_index,
        invalidated_evidence_ids=frozenset({"e1"}),
    )
    assert compiled.version == 2


def test_revision_removing_passed_required_task_without_invalidation_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    prior = graph_factory([task_factory("t1")], version=1)
    draft = graph_factory([task_factory("t2")], parent_version=1)  # t1 silently dropped

    attempts = [_passed_attempt("t1")]
    evidence_index = EvidenceIndex([_evidence("t1")])

    with pytest.raises(GraphValidationError, match="without explicit invalidation"):
        compile_and_validate(
            draft,
            request_factory(),
            node_registry,
            permission_registry,
            prior_version=prior,
            completed_attempts=attempts,
            evidence_index=evidence_index,
        )


def test_revision_altering_task_with_no_passed_attempt_needs_no_invalidation(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    prior = graph_factory([task_factory("t1")], version=1)
    draft = graph_factory([task_factory("t1", required=False)], parent_version=1)
    # no completed attempts at all -> nothing to protect
    compiled = compile_and_validate(
        draft, request_factory(), node_registry, permission_registry, prior_version=prior
    )
    assert compiled.version == 2


# --- Permission ceilings ---------------------------------------------------


def test_network_profile_without_deploy_authorization_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    tasks = [
        task_factory(
            "deploy",
            node_type="deploy_exec",
            node_kind=NodeKind.MECHANICAL,
            permission_profile="network",
        )
    ]
    graph = graph_factory(tasks)
    req = request_factory(deploy_authorized=False)
    with pytest.raises(GraphValidationError, match="network access"):
        compile_and_validate(graph, req, node_registry, permission_registry)


def test_network_profile_with_deploy_authorization_compiles(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    from forge.models.common import NodeKind

    tasks = [
        task_factory(
            "deploy",
            node_type="deploy_exec",
            node_kind=NodeKind.MECHANICAL,
            permission_profile="network",
        )
    ]
    graph = graph_factory(tasks)
    req = request_factory(deploy_authorized=True)
    compiled = compile_and_validate(graph, req, node_registry, permission_registry)
    assert compiled.status == "active"


def test_unknown_permission_profile_rejected(
    node_registry, permission_registry, request_factory, task_factory, graph_factory
):
    tasks = [task_factory("t1", permission_profile="does_not_exist")]
    graph = graph_factory(tasks)
    with pytest.raises(GraphValidationError, match="unknown permission profile"):
        compile_and_validate(graph, request_factory(), node_registry, permission_registry)
