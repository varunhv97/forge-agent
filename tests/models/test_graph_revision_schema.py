from __future__ import annotations

from forge.models.decision import GraphRevisionProposal


def test_graph_revision_proposal_cannot_mutate_permission_profile():
    """
    plan §10 test #3: self-granted permission escalation must be
    structurally impossible, not merely policy-denied. Assert via schema
    introspection that no field on GraphRevisionProposal (or on the
    TaskSpec it's allowed to add) can change an *existing* task's
    permission_profile, budgets, or acceptance criteria — the only way to
    affect those is add_tasks (whole new tasks) and remove_task_ids, never
    an in-place mutation of something already in the graph.
    """
    fields = set(GraphRevisionProposal.model_fields.keys())
    assert fields == {
        "base_graph_version_id",
        "add_tasks",
        "add_edges",
        "remove_task_ids",
        "rationale",
        "invalidated_evidence_ids",
    }
    # no field that could target an existing task id for an in-place
    # mutation of its permission_profile/budgets/acceptance criteria
    forbidden_field_names = {
        "permission_profile",
        "budgets",
        "set_permission_profile",
        "update_task",
        "patch_task",
        "mutate_task",
    }
    assert fields.isdisjoint(forbidden_field_names)
