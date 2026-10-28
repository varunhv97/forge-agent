from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.validator import compile_and_validate
from forge.models.common import Mode
from forge.models.model_registry import ModelCapability
from forge.models.permissions import Budgets
from forge.modes.custom_dag import (
    CustomDagError,
    build_custom_dag_request_and_graph,
    list_dag_templates,
)
from forge.nodes import register_all


def _registries() -> tuple[NodeTypeRegistry, PermissionProfileRegistry]:
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    register_all(node_registry, permission_registry)
    return node_registry, permission_registry


def _model_registry() -> ModelRegistry:
    """Empty, same as every live caller today (see architecture.md) — no
    task in these tests declares a model override unless noted."""
    return ModelRegistry([])


def _model_registry_with_opus() -> ModelRegistry:
    """A ModelRegistry with one real capability, eligible for
    implement_exec only — used by the model-override tests below."""
    return ModelRegistry(
        [
            ModelCapability(
                provider="anthropic",
                model_id="claude-opus-5",
                adapter_kind="sdk_execution",
                supports_tools=True,
                context_window=200000,
                cost_per_mtok_in=15.0,
                cost_per_mtok_out=75.0,
                eligible_roles=["implement_exec"],
            )
        ]
    )


def _valid_definition() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "implement-and-verify",
        "description": "Build and verify: {{ objective }}",
        "inputs": [{"name": "objective", "prompt": "What should implement_exec build?"}],
        "acceptance_criteria": [
            {
                "id": "tests_pass",
                "description": "unit tests pass for: {{ objective }}",
                "verification_method": "automated_test",
                "required": True,
            }
        ],
        "tasks": [
            {
                "id": "implement",
                "node_type": "implement_exec",
                "description": "{{ objective }}",
                "depends_on": [],
                "acceptance_criterion_ids": [],
            },
            {
                "id": "verify",
                "node_type": "unit_verify_exec",
                "description": "run the unit tests implement wrote",
                "depends_on": ["implement"],
                "acceptance_criterion_ids": ["tests_pass"],
            },
        ],
    }


def _write(tmp_path: Path, definition: dict[str, Any], name: str = "dag.json") -> Path:
    file_path = tmp_path / name
    file_path.write_text(json.dumps(definition))
    return file_path


def test_valid_custom_dag_compiles_cleanly(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    file_path = _write(tmp_path, _valid_definition())

    request, draft = build_custom_dag_request_and_graph(
        file_path,
        {"objective": "add a /health endpoint"},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )

    compiled = compile_and_validate(draft, request, node_registry, permission_registry)

    assert compiled.status == "active"
    assert [t.id for t in compiled.tasks] == ["implement", "verify"]
    assert request.mode == Mode.CUSTOM
    assert request.template_name == "implement-and-verify"
    # deploy_authorized/budgets are taken exactly as given, never invented
    assert request.deploy_authorized is False
    assert request.budgets.max_cost_usd == 5.0


def test_placeholder_substitution_applies_to_tasks_and_criteria(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    file_path = _write(tmp_path, _valid_definition())

    request, draft = build_custom_dag_request_and_graph(
        file_path,
        {"objective": "add a /health endpoint"},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )

    implement_task = next(t for t in draft.tasks if t.id == "implement")
    assert implement_task.description == "add a /health endpoint"
    assert request.acceptance_criteria[0].description == (
        "unit tests pass for: add a /health endpoint"
    )
    assert request.objective_text == "Build and verify: add a /health endpoint"


def test_missing_input_value_for_placeholder_raises_specific_error(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    file_path = _write(tmp_path, _valid_definition())

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {},  # 'objective' never supplied
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    message = str(exc_info.value)
    assert str(file_path) in message
    assert "objective" in message


def test_unknown_node_type_raises_specific_error_naming_file_and_value(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    definition = _valid_definition()
    definition["tasks"][0]["node_type"] = "totally_made_up_node_type"
    file_path = _write(tmp_path, definition)

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {"objective": "add a /health endpoint"},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    message = str(exc_info.value)
    assert str(file_path) in message
    assert "implement" in message
    assert "totally_made_up_node_type" in message


def test_unknown_permission_profile_override_raises_specific_error(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    definition = _valid_definition()
    definition["tasks"][0]["permission_profile"] = "no_such_profile"
    file_path = _write(tmp_path, definition)

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {"objective": "add a /health endpoint"},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    message = str(exc_info.value)
    assert str(file_path) in message
    assert "implement" in message
    assert "no_such_profile" in message


def test_task_omitting_permission_profile_defaults_to_node_types_own(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    file_path = _write(tmp_path, _valid_definition())

    _, draft = build_custom_dag_request_and_graph(
        file_path,
        {"objective": "add a /health endpoint"},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )

    implement_task = next(t for t in draft.tasks if t.id == "implement")
    assert (
        implement_task.permission_profile == node_registry.get("implement_exec").permission_profile
    )


# -- model/effort overrides (phase 2 of the model/effort customization arc) --


def test_task_omitting_model_and_effort_leaves_both_overrides_unset(tmp_path: Path) -> None:
    """
    No regression on pre-existing custom-DAG behavior: a task that
    declares neither field gets TaskSpec.model_override/effort_override ==
    None, exactly as if these fields didn't exist at all.
    """
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()
    file_path = _write(tmp_path, _valid_definition())

    _, draft = build_custom_dag_request_and_graph(
        file_path,
        {"objective": "add a /health endpoint"},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )

    implement_task = next(t for t in draft.tasks if t.id == "implement")
    assert implement_task.model_override is None
    assert implement_task.effort_override is None


def test_task_level_model_and_effort_overrides_land_on_the_task_spec(tmp_path: Path) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry_with_opus()
    definition = _valid_definition()
    definition["tasks"][0]["model"] = "claude-opus-5"
    definition["tasks"][0]["effort"] = "low"
    file_path = _write(tmp_path, definition)

    _, draft = build_custom_dag_request_and_graph(
        file_path,
        {"objective": "add a /health endpoint"},
        node_registry,
        permission_registry,
        model_registry,
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )

    implement_task = next(t for t in draft.tasks if t.id == "implement")
    assert implement_task.model_override == "claude-opus-5"
    assert implement_task.effort_override == "low"
    # untouched task keeps no override
    verify_task = next(t for t in draft.tasks if t.id == "verify")
    assert verify_task.model_override is None
    assert verify_task.effort_override is None


def test_unknown_model_override_raises_specific_error_naming_file_and_value(
    tmp_path: Path,
) -> None:
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()  # empty — nothing is registered at all
    definition = _valid_definition()
    definition["tasks"][0]["model"] = "totally-made-up-model"
    file_path = _write(tmp_path, definition)

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {"objective": "add a /health endpoint"},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    message = str(exc_info.value)
    assert str(file_path) in message
    assert "implement" in message
    assert "totally-made-up-model" in message


def test_model_override_ineligible_for_this_node_type_raises_specific_error(
    tmp_path: Path,
) -> None:
    """
    claude-opus-5 is registered, but only eligible for implement_exec — a
    task that references it for unit_verify_exec (a MECHANICAL node type,
    which never calls a model at all, but the check doesn't need to know
    that to reject this) must still get a specific CustomDagError, not a
    bare ModelNotEligibleError surfacing from deep inside this function.
    """
    node_registry, permission_registry = _registries()
    model_registry = _model_registry_with_opus()
    definition = _valid_definition()
    definition["tasks"][1]["model"] = "claude-opus-5"
    file_path = _write(tmp_path, definition)

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {"objective": "add a /health endpoint"},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    message = str(exc_info.value)
    assert str(file_path) in message
    assert "verify" in message
    assert "claude-opus-5" in message


def test_malformed_json_raises_specific_error(tmp_path: Path) -> None:
    file_path = tmp_path / "broken.json"
    file_path.write_text("{not valid json")
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    assert str(file_path) in str(exc_info.value)


def test_unsupported_schema_version_raises_specific_error(tmp_path: Path) -> None:
    definition = _valid_definition()
    definition["schema_version"] = 2
    file_path = _write(tmp_path, definition)
    node_registry, permission_registry = _registries()
    model_registry = _model_registry()

    with pytest.raises(CustomDagError) as exc_info:
        build_custom_dag_request_and_graph(
            file_path,
            {"objective": "x"},
            node_registry,
            permission_registry,
            model_registry,
            Budgets(max_cost_usd=5.0, max_wall_seconds=900),
        )

    assert "schema_version" in str(exc_info.value)


# -- list_dag_templates (the TUI's "new session" screen uses this) -----------


def test_list_dag_templates_empty_dir_returns_nothing(tmp_path: Path) -> None:
    assert list_dag_templates(tmp_path / "dags") == []


def test_list_dag_templates_lists_valid_templates_sorted_by_name(tmp_path: Path) -> None:
    dags_dir = tmp_path / "dags"
    dags_dir.mkdir()
    zebra = _valid_definition()
    zebra["name"] = "zebra-template"
    zebra["description"] = "the zebra one"
    apple = _valid_definition()
    apple["name"] = "apple-template"
    apple["description"] = "the apple one"
    (dags_dir / "zebra.json").write_text(json.dumps(zebra))
    (dags_dir / "apple.json").write_text(json.dumps(apple))

    templates = list_dag_templates(dags_dir)

    assert [t.name for t in templates] == ["apple-template", "zebra-template"]
    assert templates[0].description == "the apple one"
    assert templates[0].inputs[0].name == "objective"


def test_list_dag_templates_skips_a_broken_file(tmp_path: Path) -> None:
    dags_dir = tmp_path / "dags"
    dags_dir.mkdir()
    (dags_dir / "good.json").write_text(json.dumps(_valid_definition()))
    (dags_dir / "broken.json").write_text("not json at all")

    templates = list_dag_templates(dags_dir)

    assert [t.name for t in templates] == ["implement-and-verify"]
