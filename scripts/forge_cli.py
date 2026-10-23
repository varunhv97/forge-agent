#!/usr/bin/env python3
"""
Forge's CLI.

Plain-text/structural layer: `forge trace <run_id>` and
`forge inspect <run_id> [task_id]`.

Real runs: `forge run`, `forge resume`, `forge list`.

Thin by design, same split as scripts/live_feature_blast_m7.py: argument
parsing, printing, and interactive prompts live here (the one place ruff's
T20 "no print" is already scoped off, via pyproject.toml's per-file-ignores
for scripts/**); all formatting and Store lookups live in forge/cli.py,
which returns strings rather than printing them; per-project `.forge/`
discovery lives in forge/project.py.

`forge inspect` takes a run id and, if given, a task id *within that run's
graph* — task ids are only unique per graph version, not globally, so a
single bare id can't be dispatched as "run or task" without already having
a run in scope. See forge/cli.py:build_inspect_report's docstring.

Installed as the `forge` console script (see pyproject.toml's
[project.scripts]), so after an editable install this runs as:

    forge                    # launches the interactive TUI (forge/tui/*)
    forge trace <run_id>
    forge inspect <run_id> [task_id]
    forge run <objective> --journey "<text>" --deploy-authorized \
        [--mode feature-blast|<custom-dag-name>] [--db PATH]
    forge resume <run_id>
    forge list

Bare `forge` (no subcommand) hands off to `forge.tui.app.run_tui` — a
full-screen Textual shell around the same `.forge/` discovery and
run/resume machinery the one-shot commands above use.

or, without installing, directly as `python scripts/forge_cli.py ...`.

`forge run`/`resume`/`list` resolve their database from the enclosing git
repo's `.forge/forge.db` (creating `.forge/` if needed, prompting to
`git init` if the cwd isn't a git repo at all) unless `--db` overrides it;
`trace`/`inspect` keep their original default of `./forge.db`, unchanged
from M8.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge import project
from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter
from forge.adapters.execution.sdk_node_runner import run_sdk_node
from forge.cli import (
    TaskNotFoundError,
    build_inspect_report,
    build_list_runs_report,
    build_run_attempts_summary,
    build_trace_report,
)
from forge.controller import db
from forge.controller.execution_registry import ExecutionHandlerRegistry
from forge.controller.mechanical import MechanicalRunnerRegistry
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import ControllerDependencies, run_to_completion, start_run
from forge.controller.store import NotFoundError, Store
from forge.controller.validator import GraphValidationError, compile_and_validate
from forge.models.permissions import Budgets
from forge.modes.custom_dag import (
    CustomDagError,
    build_custom_dag_request_and_graph,
    load_dag_definition,
)
from forge.modes.feature_blast import build_feature_blast_request_and_graph
from forge.nodes import register_all
from forge.tui.app import run_tui

DEFAULT_DB_PATH = Path("forge.db")
# forge run's own default Budgets when none is configurable yet via a CLI
# flag — a caller-level default, not one forge/modes/custom_dag.py or
# feature_blast.py invents for themselves. Matches feature_blast's own
# internal default so the two modes behave the same unless something
# explicitly differs.
DEFAULT_RUN_BUDGETS = Budgets(max_cost_usd=10.0, max_wall_seconds=1800)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "path to the Forge SQLite database. For run/resume/list, defaults "
            "to the enclosing git repo's .forge/forge.db (discovered from the "
            f"cwd); for trace/inspect, defaults to '{DEFAULT_DB_PATH}'."
        ),
    )
    # Not required: a bare `forge` (no subcommand at all) launches the
    # interactive TUI instead (see main()) rather than erroring out of
    # argparse with "the following arguments are required: command".
    subparsers = parser.add_subparsers(dest="command", required=False)

    trace_parser = subparsers.add_parser(
        "trace", help="replay a run's full audit trail, in chronological order"
    )
    trace_parser.add_argument("run_id")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="show a structural snapshot of a run, or of one task within it",
    )
    inspect_parser.add_argument("run_id")
    inspect_parser.add_argument(
        "task_id",
        nargs="?",
        default=None,
        help="if given, inspect this task within the run's active graph instead of the run itself",
    )

    run_parser = subparsers.add_parser("run", help="start a new run")
    run_parser.add_argument("objective")
    run_parser.add_argument(
        "--journey",
        default=None,
        help="the end-to-end journey to verify once deployed (required for --mode feature-blast)",
    )
    run_parser.add_argument(
        "--deploy-authorized",
        action="store_true",
        help="required: forge run refuses to start without this",
    )
    run_parser.add_argument(
        "--mode",
        default="feature-blast",
        help=(
            "'feature-blast' (default), or the name of a custom DAG template "
            "loaded from .forge/dags/<name>.json"
        ),
    )

    resume_parser = subparsers.add_parser(
        "resume", help="resume an existing run (re-enters run_to_completion)"
    )
    resume_parser.add_argument("run_id")

    subparsers.add_parser("list", help="list every run, newest-updated first")

    return parser


def _prompt_git_init(cwd: Path) -> bool:
    answer = input(
        f"Not a git repo: {cwd}. Forge needs a git repo to isolate work in "
        f"worktrees. Work out of this folder and run `git init` here? [y/n] "
    )
    return answer.strip().lower() in ("y", "yes")


def _resolve_project(args: argparse.Namespace) -> tuple[Path, Path] | None:
    """
    Resolves (repo_root, db_path) for forge run/resume/list.

    `repo_root` always comes from the enclosing git repo — it's needed for
    worktrees regardless of whether `--db` overrides the database path.
    Returns None if the cwd isn't a git repo and the user declines the
    interactive `git init` prompt; the caller should then exit cleanly
    (status 0, nothing created).
    """
    try:
        repo_root = project.find_repo_root()
    except project.NotAGitRepoError as exc:
        if not _prompt_git_init(exc.cwd):
            return None
        repo_root = project.init_repo(exc.cwd)

    paths = project.ensure_forge_dir(repo_root)
    db_path = args.db if args.db is not None else paths.db_path
    return repo_root, db_path


def _build_registries() -> tuple[
    NodeTypeRegistry,
    PermissionProfileRegistry,
    MechanicalRunnerRegistry,
    ExecutionHandlerRegistry,
]:
    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    mechanical_registry = MechanicalRunnerRegistry()
    execution_handler_registry = ExecutionHandlerRegistry()
    register_all(
        node_registry,
        permission_registry,
        mechanical_registry,
        execution_handler_registry,
        deploy_adapter=VercelDeployAdapter(),
    )
    return node_registry, permission_registry, mechanical_registry, execution_handler_registry


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.deploy_authorized:
        print("error: forge run refuses to start without --deploy-authorized", file=sys.stderr)
        return 1

    resolved = _resolve_project(args)
    if resolved is None:
        return 0
    repo_root, db_path = resolved

    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _build_registries()
    )

    if args.mode == "feature-blast":
        if not args.journey:
            print("error: --journey is required for --mode feature-blast", file=sys.stderr)
            return 1
        request, draft = build_feature_blast_request_and_graph(
            objective=args.objective, journey_description=args.journey
        )
    else:
        dag_path = repo_root / ".forge" / "dags" / f"{args.mode}.json"
        try:
            definition = load_dag_definition(dag_path)
            input_values: dict[str, str] = {"objective": args.objective}
            if args.journey:
                input_values["journey"] = args.journey
            for declared_input in definition.inputs:
                if declared_input.name in input_values:
                    continue
                input_values[declared_input.name] = input(f"{declared_input.prompt} ")
            request, draft = build_custom_dag_request_and_graph(
                dag_path,
                input_values,
                node_registry,
                permission_registry,
                DEFAULT_RUN_BUDGETS,
                deploy_authorized=args.deploy_authorized,
            )
        except CustomDagError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        graph = compile_and_validate(draft, request, node_registry, permission_registry)
    except GraphValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        deps = ControllerDependencies(
            mechanical_registry=mechanical_registry,
            node_registry=node_registry,
            permission_registry=permission_registry,
            model_registry=ModelRegistry([]),
            execution_handler_registry=execution_handler_registry,
            execution_runner=run_sdk_node,
            repo_root=repo_root,
        )
        run = start_run(request, graph, store)
        print(f"Started run {run.id} (request {request.id})")
        print(
            "Running to completion — this may call a real `claude` CLI session "
            "and, for a deploying mode, a real Vercel deployment...\n"
        )
        run_to_completion(run.id, store, deps)
        print(build_run_attempts_summary(store, run.id))
    finally:
        store.close()
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    resolved = _resolve_project(args)
    if resolved is None:
        return 0
    repo_root, db_path = resolved

    node_registry, permission_registry, mechanical_registry, execution_handler_registry = (
        _build_registries()
    )

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        deps = ControllerDependencies(
            mechanical_registry=mechanical_registry,
            node_registry=node_registry,
            permission_registry=permission_registry,
            model_registry=ModelRegistry([]),
            execution_handler_registry=execution_handler_registry,
            execution_runner=run_sdk_node,
            repo_root=repo_root,
        )
        run_to_completion(args.run_id, store, deps)
        print(build_run_attempts_summary(store, args.run_id))
    except NotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    resolved = _resolve_project(args)
    if resolved is None:
        return 0
    _, db_path = resolved

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        print(build_list_runs_report(store))
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.command is None:
        # Bare `forge` (optionally with `--db`) launches the interactive
        # TUI instead of printing argparse's help text.
        return run_tui(args.db)

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "list":
        return _cmd_list(args)

    # trace / inspect: unchanged from M8 other than --db's default now being
    # resolved explicitly here rather than via argparse (run/resume/list need
    # `None` to mean "not given" so they can fall back to .forge/ discovery).
    db_path = args.db if args.db is not None else DEFAULT_DB_PATH
    node_registry = NodeTypeRegistry()
    register_all(node_registry, PermissionProfileRegistry())

    conn = db.connect(db_path)
    store = Store(conn)
    try:
        if args.command == "trace":
            print(build_trace_report(store, args.run_id))
        else:
            print(build_inspect_report(store, node_registry, args.run_id, args.task_id))
    except (NotFoundError, TaskNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
