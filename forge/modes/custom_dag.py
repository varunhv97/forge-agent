"""
Loads a user-authored custom DAG template (`.forge/dags/<name>.json`) and
produces a `(Request, GraphVersion)` draft pair, exactly like
`forge/modes/feature_blast.py`'s `build_feature_blast_request_and_graph`
does — the caller is still responsible for running the returned draft
through `compile_and_validate`, the same enforcement path every mode goes
through. This is not a new mode in the `Mode` enum sense: every custom DAG
uses `Mode.CUSTOM`, and `Request.template_name` is what actually names
which template produced a given request — see forge/models/common.py's
`Mode` docstring.

A custom DAG arranges *existing* node types into a new graph shape; it
never defines new node behavior. Accordingly this module validates every
task's `node_type`/`permission_profile` against the real registries passed
in and raises a specific `CustomDagError` (naming the file and the exact
bad value) rather than letting a typo surface deep inside
`compile_and_validate` as a generic `GraphValidationError` with no
indication of which JSON file or field caused it.

Deliberately out of scope here: `deploy_authorized` and `Budgets` are
caller-supplied/run-time values (requirements.md's M9 section is explicit
about this) — this module never invents or defaults either one itself.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from forge.controller.node_registry import (
    NodeTypeRegistry,
    PermissionProfileRegistry,
    UnknownNodeTypeError,
    UnknownPermissionProfileError,
)
from forge.models.common import Mode
from forge.models.graph import EdgeSpec, GraphVersion, TaskSpec
from forge.models.permissions import Budgets
from forge.models.request import AcceptanceCriterion, Request


class CustomDagError(Exception):
    """
    Raised for anything wrong with a custom DAG template file itself — an
    unparseable file, a schema mismatch, an unknown `node_type` or
    `permission_profile`, or a placeholder referencing an undeclared
    input. Every message names the file path and the specific offending
    value; none of this is meant to surface as a generic
    `GraphValidationError` from deep inside `compile_and_validate`.
    """


_SUPPORTED_SCHEMA_VERSION = 1


class DagInputSpec(BaseModel):
    name: str
    prompt: str


class DagAcceptanceCriterionSpec(BaseModel):
    id: str
    description: str
    verification_method: Literal[
        "automated_test", "manual_journey", "mechanical_check", "deployed_probe"
    ]
    required: bool = True


class DagTaskSpec(BaseModel):
    id: str
    node_type: str
    description: str
    depends_on: list[str] = []
    acceptance_criterion_ids: list[str] = []
    # Optional: when omitted, defaults to the node_type's own
    # NodeType.permission_profile — see _resolve_permission_profile. The
    # file never has to repeat a node type's default profile just to name
    # a task.
    permission_profile: str | None = None
    required: bool = True


class DagDefinition(BaseModel):
    schema_version: int
    name: str
    description: str = ""
    inputs: list[DagInputSpec] = []
    acceptance_criteria: list[DagAcceptanceCriterionSpec]
    tasks: list[DagTaskSpec]
    # tasks[].depends_on is deliberately the only dependency-authoring
    # surface in this pass: every dependency edge is derived straight from
    # it (relation="dependency"). A separate section for context_only
    # edges (artifacts passed without a scheduling gate) isn't supported
    # yet — that's a real simplification, not a forgotten feature, and
    # can be added later without changing this shape.


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _substitute(text: str, input_values: dict[str, str], *, file_path: Path) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in input_values:
            raise CustomDagError(
                f"{file_path}: placeholder '{{{{ {name} }}}}' references an input "
                f"'{name}' that isn't declared in this template's 'inputs' "
                f"(or wasn't supplied)"
            )
        return input_values[name]

    return _PLACEHOLDER_RE.sub(_replace, text)


def load_dag_definition(file_path: Path) -> DagDefinition:
    """
    Reads and parses one `.forge/dags/*.json` file into a `DagDefinition`.
    Every failure mode — the file missing/unreadable, invalid JSON, or
    JSON that doesn't match the expected schema — raises `CustomDagError`
    naming `file_path`, never a bare `OSError`/`ValidationError` with no
    indication of which file was being loaded.
    """
    try:
        raw = json.loads(file_path.read_text())
    except OSError as exc:
        raise CustomDagError(f"{file_path}: failed to read custom DAG file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CustomDagError(f"{file_path}: not valid JSON: {exc}") from exc

    try:
        definition = DagDefinition.model_validate(raw)
    except ValidationError as exc:
        raise CustomDagError(f"{file_path}: doesn't match the custom DAG schema: {exc}") from exc

    if definition.schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise CustomDagError(
            f"{file_path}: unsupported schema_version {definition.schema_version} "
            f"(expected {_SUPPORTED_SCHEMA_VERSION})"
        )
    return definition


@dataclass(frozen=True)
class DagTemplateInfo:
    """
    Enough to list a custom DAG template as a selectable option (e.g. in
    the TUI's "new session" screen) without the caller needing to load and
    hold onto the full `DagDefinition` itself.
    """

    path: Path
    name: str
    description: str
    inputs: list[DagInputSpec]


def list_dag_templates(dags_dir: Path) -> list[DagTemplateInfo]:
    """
    Lists every loadable `.forge/dags/*.json` template in `dags_dir`,
    sorted by name. A file that fails to parse or validate (a bad
    schema_version, invalid JSON, whatever `load_dag_definition` itself
    would raise `CustomDagError` for) is skipped rather than propagating —
    one broken template file shouldn't stop every other one from being
    offered. Returns an empty list if `dags_dir` doesn't exist at all,
    which is the ordinary case for a project with no custom DAGs yet.
    """
    if not dags_dir.is_dir():
        return []
    templates = []
    for path in dags_dir.glob("*.json"):
        try:
            definition = load_dag_definition(path)
        except CustomDagError:
            continue
        templates.append(
            DagTemplateInfo(
                path=path,
                name=definition.name,
                description=definition.description,
                inputs=list(definition.inputs),
            )
        )
    templates.sort(key=lambda t: t.name)
    return templates


def _resolve_permission_profile(
    task: DagTaskSpec,
    node_type_name: str,
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    *,
    file_path: Path,
) -> str:
    profile_name = task.permission_profile or node_registry.get(node_type_name).permission_profile
    try:
        permission_registry.get(profile_name)
    except UnknownPermissionProfileError as exc:
        raise CustomDagError(
            f"{file_path}: task '{task.id}' references unknown permission_profile "
            f"'{profile_name}'"
        ) from exc
    return profile_name


def build_custom_dag_request_and_graph(
    file_path: Path,
    input_values: dict[str, str],
    node_registry: NodeTypeRegistry,
    permission_registry: PermissionProfileRegistry,
    budgets: Budgets,
    *,
    deploy_authorized: bool = False,
    request_id: str | None = None,
    graph_version_id: str | None = None,
) -> tuple[Request, GraphVersion]:
    """
    Loads the custom DAG template at `file_path` and builds the `Request`
    and a draft `GraphVersion` for it — the caller still runs the result
    through `compile_and_validate` itself, same as
    `build_feature_blast_request_and_graph`.

    `input_values` is the `{input_name: value}` mapping collected by the
    caller (the CLI layer prompts for each declared `input`; this function
    never prompts for anything itself) and is used to substitute every
    `{{ input_name }}` placeholder found in a task's or acceptance
    criterion's `description`.

    `budgets` and `deploy_authorized` are taken exactly as given — this
    module never invents a default for either, since both are run-time,
    caller-owned decisions (see this module's docstring).
    """
    definition = load_dag_definition(file_path)

    tasks: list[TaskSpec] = []
    edges: list[EdgeSpec] = []
    for dag_task in definition.tasks:
        try:
            node_type = node_registry.get(dag_task.node_type)
        except UnknownNodeTypeError as exc:
            raise CustomDagError(
                f"{file_path}: task '{dag_task.id}' references unknown node_type "
                f"'{dag_task.node_type}'"
            ) from exc

        permission_profile = _resolve_permission_profile(
            dag_task,
            dag_task.node_type,
            node_registry,
            permission_registry,
            file_path=file_path,
        )
        description = _substitute(dag_task.description, input_values, file_path=file_path)

        tasks.append(
            TaskSpec(
                id=dag_task.id,
                description=description,
                node_kind=node_type.kind,
                node_type=dag_task.node_type,
                depends_on=list(dag_task.depends_on),
                acceptance_criterion_ids=list(dag_task.acceptance_criterion_ids),
                permission_profile=permission_profile,
                required=dag_task.required,
            )
        )
        edges.extend(
            EdgeSpec(from_task=dep, to_task=dag_task.id, relation="dependency")
            for dep in dag_task.depends_on
        )

    acceptance_criteria = [
        AcceptanceCriterion(
            id=criterion.id,
            description=_substitute(criterion.description, input_values, file_path=file_path),
            verification_method=criterion.verification_method,
            required=criterion.required,
        )
        for criterion in definition.acceptance_criteria
    ]

    request_id = request_id or f"req-{uuid.uuid4().hex[:8]}"
    graph_version_id = graph_version_id or f"gv-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)

    request = Request(
        id=request_id,
        mode=Mode.CUSTOM,
        template_name=definition.name,
        objective_text=_substitute(
            definition.description or definition.name, input_values, file_path=file_path
        ),
        acceptance_criteria=acceptance_criteria,
        deploy_authorized=deploy_authorized,
        budgets=budgets,
        created_at=now,
    )
    draft = GraphVersion(
        id=graph_version_id,
        request_id=request.id,
        version=1,
        parent_version=None,
        tasks=tasks,
        edges=edges,
        created_by="template",
        created_at=now,
    )
    return request, draft
