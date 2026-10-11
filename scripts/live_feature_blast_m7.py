#!/usr/bin/env python3
"""
Live end-to-end verification for Feature Blast's M7 descoped first slice:

    implement_exec -> unit_verify_exec -> deploy_exec -> manual_test_exec

Builds a disposable toy Python app in a scratch git repo, asks implement_exec
to add a small new serverless endpoint to it, verifies the resulting unit
tests, deploys the result to a real Vercel project, and has manual_test_exec
drive a real HTTP check against the live deployed URL. Everything here is
real: a real `claude` CLI session for implement_exec and manual_test_exec, a
real git worktree/commit history, and a real Vercel deployment — nothing in
this path is mocked.

This needs:
  - An authenticated `claude` CLI session (same requirement as
    scripts/live_smoke_test.py).
  - A `VERCEL_TOKEN` in `.env.local` at the repo root (gitignored).

Run it from your own terminal, not from Forge's sandboxed dev environment:

    source .venv/bin/activate
    python scripts/live_feature_blast_m7.py
    python scripts/live_feature_blast_m7.py --keep
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from forge.adapters.deploy.vercel_adapter import VercelDeployAdapter  # noqa: E402
from forge.adapters.execution.sdk_node_runner import run_sdk_node  # noqa: E402
from forge.controller import db  # noqa: E402
from forge.controller.execution_registry import ExecutionHandlerRegistry  # noqa: E402
from forge.controller.mechanical import MechanicalRunnerRegistry  # noqa: E402
from forge.controller.model_registry import ModelRegistry  # noqa: E402
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry  # noqa: E402
from forge.controller.scheduler import (
    ControllerDependencies,
    run_to_completion,
    start_run,
)  # noqa: E402
from forge.controller.store import Store  # noqa: E402
from forge.controller.validator import compile_and_validate  # noqa: E402
from forge.models.run import RunStatus  # noqa: E402
from forge.modes.feature_blast import build_feature_blast_request_and_graph  # noqa: E402
from forge.nodes import register_all  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_LOCAL_PATH = REPO_ROOT / ".env.local"

OBJECTIVE = (
    "This is a Vercel Python serverless app. Add a new file api/greet.py "
    "exposing a GET endpoint: it must define a module-level class named "
    "`handler` that subclasses `http.server.BaseHTTPRequestHandler` (the "
    "Vercel Python runtime convention already used by api/index.py in this "
    "repo — look at that file first). On GET, read a `name` query parameter "
    "from the request path (default to 'world' if absent), respond with "
    'status 200 and a JSON body {"message": "hello, <name>!"}. Add a '
    "unit test for the JSON-building logic specifically (factor it into a "
    "plain function you can call directly from a test, since "
    "BaseHTTPRequestHandler itself isn't easily unit-testable)."
)
JOURNEY_DESCRIPTION = (
    "GET /api/greet?name=Forge on the deployed URL returns HTTP 200 with a "
    'JSON body whose "message" field contains the text "hello, Forge!"'
)


def _load_env_local() -> None:
    """
    Loads KEY=VALUE pairs from .env.local into os.environ, without ever
    printing or logging a value — this script only needs VERCEL_TOKEN to
    reach the environment so VercelDeployAdapter can resolve it itself.
    """
    if not ENV_LOCAL_PATH.exists():
        return
    for line in ENV_LOCAL_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_toy_repo(path: Path) -> None:
    """
    A minimal Vercel Python serverless app: one baseline endpoint
    (api/index.py) and a baseline test, establishing the convention
    implement_exec is asked to follow for the new endpoint it adds. The new
    endpoint itself (api/greet.py) is deliberately NOT scaffolded here —
    that is the actual work implement_exec is being tested on.
    """
    path.mkdir(parents=True)
    api_dir = path / "api"
    api_dir.mkdir()

    (api_dir / "index.py").write_text(
        '"""Vercel Python serverless entrypoint for the baseline /api/index route."""\n'
        "import json\n"
        "from http.server import BaseHTTPRequestHandler\n"
        "\n"
        "\n"
        "def build_status_body() -> dict:\n"
        '    return {"status": "ok"}\n'
        "\n"
        "\n"
        "class handler(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200)\n"
        '        self.send_header("Content-Type", "application/json")\n'
        "        self.end_headers()\n"
        "        self.wfile.write(json.dumps(build_status_body()).encode())\n"
    )
    (path / "test_index.py").write_text(
        "from api.index import build_status_body\n"
        "\n"
        "\n"
        "def test_build_status_body():\n"
        '    assert build_status_body() == {"status": "ok"}\n'
    )
    (path / "vercel.json").write_text('{"version": 2}\n')
    (path / "README.md").write_text("Forge Feature Blast M7 scratch toy app.\n")

    _git(path, "init")
    _git(path, "config", "user.email", "forge-m7@example.com")
    _git(path, "config", "user.name", "Forge M7 Live Test")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")


def probe_deployed_journey(base_url: str) -> tuple[bool, str]:
    """
    An independent check against the live deployed URL, never trusting the
    run's own COMPLETED_VERIFIED status alone — mirrors manual_test_exec's
    own check but runs outside the controller entirely, as a second,
    external confirmation.
    """
    url = f"{base_url}/api/greet?name=Forge"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            body = response.read().decode()
    except urllib.error.URLError as exc:
        return False, f"request to {url} failed: {exc}"
    if "hello, forge!" not in body.lower():
        return False, f"unexpected response body from {url}: {body!r}"
    return True, f"{url} -> {body!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep", action="store_true", help="don't delete the scratch repo/db afterward"
    )
    args = parser.parse_args()

    _load_env_local()
    if not os.environ.get("VERCEL_TOKEN"):
        print("VERCEL_TOKEN is not set (checked .env.local and the environment).", file=sys.stderr)
        return 1

    scratch_dir = Path(tempfile.mkdtemp(prefix="forge-m7-"))
    repo_root = scratch_dir / "repo"
    print(f"Scratch repo: {repo_root}")
    make_toy_repo(repo_root)

    conn = db.connect(scratch_dir / "forge.db")
    store = Store(conn)

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

    request, draft = build_feature_blast_request_and_graph(
        objective=OBJECTIVE, journey_description=JOURNEY_DESCRIPTION
    )
    graph = compile_and_validate(draft, request, node_registry, permission_registry)

    deps = ControllerDependencies(
        mechanical_registry=mechanical_registry,
        node_registry=node_registry,
        permission_registry=permission_registry,
        model_registry=ModelRegistry([]),
        execution_handler_registry=execution_handler_registry,
        execution_runner=run_sdk_node,
        repo_root=repo_root,
    )

    print(f"Objective: {OBJECTIVE}\n")
    print("Starting run — this makes real calls to your authenticated `claude` CLI")
    print("and a real Vercel deployment...\n")
    run = start_run(request, graph, store)
    result = run_to_completion(run.id, store, deps, max_ticks=30)

    print(f"\nFinal run status: {result.status.value}")
    deployment_url = None
    for attempt in sorted(store.load_attempts(run.id), key=lambda a: (a.task_id, a.attempt_number)):
        print(
            f"  {attempt.task_id} attempt#{attempt.attempt_number}: {attempt.status.value} "
            f"(worktree={attempt.worktree_path or '-'})"
        )
        if attempt.failure_summary:
            print(f"    failure: {attempt.failure_summary}")
        if attempt.result_commit:
            print(f"    result_commit: {attempt.result_commit}")
        if attempt.task_id == "deploy" and attempt.outputs.get("deployment_url"):
            deployment_url = attempt.outputs["deployment_url"]

    exit_code = 0 if result.status == RunStatus.COMPLETED_VERIFIED else 1

    if deployment_url:
        base_url = f"https://{deployment_url}"
        print(f"\nIndependently probing the live deployment at {base_url} ...")
        probe_passed, probe_detail = probe_deployed_journey(base_url)
        print(f"  {'PASSED' if probe_passed else 'FAILED'}: {probe_detail}")
        if not probe_passed:
            exit_code = 1
    else:
        print("\nNo deployment URL recorded — skipping the independent live probe.")
        exit_code = 1

    conn.close()
    if args.keep:
        print(f"\nScratch repo kept at: {repo_root}")
    else:
        shutil.rmtree(scratch_dir, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
