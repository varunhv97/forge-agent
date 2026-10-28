from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import scripts.forge_cli as forge_cli
from forge import project
from forge.controller import db
from forge.controller.execution_registry import ExecutionRunOutcome
from forge.controller.model_registry import ModelRegistry
from forge.controller.node_registry import NodeTypeRegistry, PermissionProfileRegistry
from forge.controller.scheduler import start_run
from forge.controller.store import Store
from forge.controller.validator import compile_and_validate
from forge.models.permissions import Budgets
from forge.models.run import RunStatus
from forge.modes.custom_dag import build_custom_dag_request_and_graph
from forge.nodes import register_all


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


_DAG_DEFINITION = {
    "schema_version": 1,
    "name": "implement-and-verify",
    "description": "{{ objective }}",
    "inputs": [{"name": "objective", "prompt": "What should implement_exec build?"}],
    "acceptance_criteria": [
        {
            "id": "tests_pass",
            "description": "unit tests pass",
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


def _write_dag(repo_root: Path, name: str = "implement-and-verify") -> None:
    dags_dir = repo_root / ".forge" / "dags"
    dags_dir.mkdir(parents=True, exist_ok=True)
    (dags_dir / f"{name}.json").write_text(json.dumps(_DAG_DEFINITION))


def _fake_execution_runner(
    model, prompt, profile, worktree_path, *, effort=None
) -> ExecutionRunOutcome:
    """
    Stands in for a real `claude` CLI session the same way
    tests/controller/test_execution_dispatch.py's fakes do: writes real
    work into the worktree (so the mechanical auto-commit safety net has
    something to commit) without ever touching a real LLM.
    """
    Path(worktree_path, "test_ok.py").write_text("def test_ok():\n    assert True\n")
    return ExecutionRunOutcome(completed=True, summary="wrote a passing test")


# -- forge run: gating -------------------------------------------------------


def test_run_refuses_without_deploy_authorized(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = forge_cli.main(["run", "do something"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--deploy-authorized" in captured.err


def test_run_feature_blast_requires_journey(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)

    exit_code = forge_cli.main(["run", "do something", "--deploy-authorized"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "--journey" in captured.err


# -- .forge/ discovery prompt, via `forge list` (no run execution needed) ----


def test_list_declines_git_init_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    exit_code = forge_cli.main(["list"])

    assert exit_code == 0
    assert not (not_a_repo / ".forge").exists()
    assert not (not_a_repo / ".git").exists()


def test_list_accepts_git_init_and_creates_forge_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    exit_code = forge_cli.main(["list"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert (fresh / ".git").is_dir()
    assert (fresh / ".forge" / "forge.db").exists()
    assert "(no runs)" in captured.out


# -- forge run / resume: real end-to-end against a stubbed execution runner --


def test_run_custom_dag_end_to_end(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(forge_cli, "run_sdk_node", _fake_execution_runner)
    _write_dag(git_repo)

    exit_code = forge_cli.main(
        ["run", "build a tiny thing", "--deploy-authorized", "--mode", "implement-and-verify"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Started run" in captured.out
    assert "Final run status: completed_verified" in captured.out

    conn = db.connect(git_repo / ".forge" / "forge.db")
    store = Store(conn)
    runs = store.list_runs()
    store.close()
    assert len(runs) == 1
    assert runs[0].status == RunStatus.COMPLETED_VERIFIED


def test_resume_drives_a_stalled_run_to_completion(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    Simulates a run started by a prior `forge run` invocation that had no
    execution runner configured (e.g. the process was interrupted before
    ever dispatching anything) and is still ACTIVE/stalled. `forge resume`
    must pick it up and drive it to completion.
    """
    monkeypatch.chdir(git_repo)
    paths = project.ensure_forge_dir(git_repo)
    _write_dag(git_repo)

    node_registry = NodeTypeRegistry()
    permission_registry = PermissionProfileRegistry()
    register_all(node_registry, permission_registry)
    request, draft = build_custom_dag_request_and_graph(
        git_repo / ".forge" / "dags" / "implement-and-verify.json",
        {"objective": "build a tiny thing"},
        node_registry,
        permission_registry,
        ModelRegistry([]),
        Budgets(max_cost_usd=5.0, max_wall_seconds=900),
    )
    graph = compile_and_validate(draft, request, node_registry, permission_registry)

    conn = db.connect(paths.db_path)
    store = Store(conn)
    run = start_run(request, graph, store)
    store.close()

    monkeypatch.setattr(forge_cli, "run_sdk_node", _fake_execution_runner)

    exit_code = forge_cli.main(["resume", run.id])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Final run status: completed_verified" in captured.out

    conn = db.connect(paths.db_path)
    store = Store(conn)
    resumed = store.load_run(run.id)
    store.close()
    assert resumed.status == RunStatus.COMPLETED_VERIFIED


def test_resume_unknown_run_reports_error(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)

    exit_code = forge_cli.main(["resume", "ghost-run"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "error:" in captured.err


# -- forge list ----------------------------------------------------------------


def test_list_prints_every_run_newest_first(
    monkeypatch: pytest.MonkeyPatch, git_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(forge_cli, "run_sdk_node", _fake_execution_runner)
    _write_dag(git_repo)

    exit_code = forge_cli.main(
        ["run", "first objective", "--deploy-authorized", "--mode", "implement-and-verify"]
    )
    assert exit_code == 0
    capsys.readouterr()

    exit_code = forge_cli.main(["list"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "RUN ID" in captured.out
    assert "completed_verified" in captured.out
    assert "first objective" in captured.out
