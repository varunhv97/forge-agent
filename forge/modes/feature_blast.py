from __future__ import annotations

import uuid
from datetime import datetime, timezone

from forge.models.common import Mode, NodeKind
from forge.models.graph import EdgeSpec, GraphVersion, TaskSpec
from forge.models.permissions import Budgets
from forge.models.request import AcceptanceCriterion, Request

# This is Feature Blast's descoped first slice, not the full template from
# the project plan (plan_decision expanding per-journey-step implement/
# verify tasks, review_decision, parallel branches, integration merge). It
# builds exactly one deterministic chain:
#
#   implement_exec -> unit_verify_exec -> deploy_exec -> manual_test_exec
#
# with no LLM-driven planning step — the shape itself is fixed by this
# function, not proposed by a plan_decision node. That expansion is real
# future work, not represented here yet.

_UNIT_TESTS_PASS_CRITERION_ID = "unit_tests_pass"
_DEPLOYED_CRITERION_ID = "deployed"
_JOURNEY_WORKS_CRITERION_ID = "journey_works"


def build_feature_blast_request_and_graph(
    objective: str,
    journey_description: str,
    *,
    request_id: str | None = None,
    graph_version_id: str | None = None,
    budgets: Budgets | None = None,
) -> tuple[Request, GraphVersion]:
    """
    Builds the `Request` and a draft `GraphVersion` for Feature Blast's
    descoped first slice. The caller is responsible for running the
    returned draft through `compile_and_validate` — this function only
    assembles it, exactly like a decision node's `GraphRevisionProposal`
    would, so the same enforcement path applies to a template-generated
    graph as to any other.

    `objective` describes the feature itself (what implement_exec builds).
    `journey_description` describes the end-to-end journey manual_test_exec
    verifies against the deployed URL — these are kept separate because
    they answer different questions ("what to build" vs. "what to check
    once it's live").
    """
    request_id = request_id or f"req-{uuid.uuid4().hex[:8]}"
    graph_version_id = graph_version_id or f"gv-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    acceptance_criteria = [
        AcceptanceCriterion(
            id=_UNIT_TESTS_PASS_CRITERION_ID,
            description="unit tests pass",
            verification_method="automated_test",
        ),
        AcceptanceCriterion(
            id=_DEPLOYED_CRITERION_ID,
            description="the feature is deployed",
            verification_method="mechanical_check",
        ),
        AcceptanceCriterion(
            id=_JOURNEY_WORKS_CRITERION_ID,
            description=journey_description,
            verification_method="manual_journey",
        ),
    ]
    request = Request(
        id=request_id,
        mode=Mode.FEATURE_BLAST,
        objective_text=objective,
        acceptance_criteria=acceptance_criteria,
        deploy_authorized=True,
        budgets=budgets or Budgets(max_cost_usd=10.0, max_wall_seconds=1800),
        created_at=now,
    )

    implement = TaskSpec(
        id="implement",
        description=objective,
        node_kind=NodeKind.EXECUTION,
        node_type="implement_exec",
        acceptance_criterion_ids=[_UNIT_TESTS_PASS_CRITERION_ID],
        permission_profile="code_edit_default",
    )
    unit_verify = TaskSpec(
        id="unit_verify",
        description="run the unit tests implement_exec wrote",
        node_kind=NodeKind.MECHANICAL,
        node_type="unit_verify_exec",
        depends_on=["implement"],
        acceptance_criterion_ids=[_UNIT_TESTS_PASS_CRITERION_ID],
        permission_profile="mechanical_default",
    )
    deploy = TaskSpec(
        id="deploy",
        description="deploy the verified feature",
        node_kind=NodeKind.MECHANICAL,
        node_type="deploy_exec",
        depends_on=["unit_verify"],
        acceptance_criterion_ids=[_DEPLOYED_CRITERION_ID],
        permission_profile="deploy_default",
    )
    manual_test = TaskSpec(
        id="manual_test",
        description=journey_description,
        node_kind=NodeKind.EXECUTION,
        node_type="manual_test_exec",
        depends_on=["deploy"],
        acceptance_criterion_ids=[_JOURNEY_WORKS_CRITERION_ID],
        permission_profile="manual_test_default",
    )

    draft = GraphVersion(
        id=graph_version_id,
        request_id=request.id,
        version=1,
        parent_version=None,
        tasks=[implement, unit_verify, deploy, manual_test],
        edges=[
            EdgeSpec(from_task="implement", to_task="unit_verify"),
            EdgeSpec(from_task="unit_verify", to_task="deploy"),
            EdgeSpec(from_task="deploy", to_task="manual_test"),
        ],
        created_by="template",
        created_at=now,
    )
    return request, draft
