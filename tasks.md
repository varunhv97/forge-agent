# Roadmap

Milestones are built in order, since each one depends on the previous. A
milestone counts as done only when its acceptance criterion has been checked
independently (passing tests, or a live run for the live-verified ones).
Commit messages hold the full history; this file is a short index.

- [x] **M0: Core models and graph validator.** Pydantic models,
      `NodeTypeRegistry`/`PermissionProfileRegistry`, `compile_and_validate`
      (acyclicity plus type, dependency, gate, invalidation and permission
      checks).
- [x] **M1: Controller, SQLite store, mechanical nodes, resumability.**
      `Run`/`TraceEvent` models, a WAL-mode SQLite `Store`,
      `MechanicalRunnerRegistry`, the scheduler (readiness, dispatch,
      `all_required_gates_met`) and orphaned-attempt reconciliation on
      restart. Includes a simulated crash-and-resume test.
- [x] **M2: Worktree isolation, Claude Agent SDK adapter, PreToolUse hooks.**
      `forge/isolation/worktree_manager.py`, the adapter-agnostic policy
      engine in `hooks.py`, and its wiring into real `claude_agent_sdk` types
      in `sdk_execution_adapter.py`. Requires Python 3.11.
- [x] **M3: Decision adapter, bounded `DecisionAction`, `apply_decision`.**
      An Anthropic API decision adapter with forced tool-use structured output,
      tested offline via `httpx.MockTransport`. Also adds `select_model` plus
      eligibility checks, `apply_decision` (the only place a proposal becomes
      a state change), deterministic fallback policies, and scheduler support
      for driving failed tasks through a decision every tick. Bug fixed:
      `_has_unrecoverable_failure` checked whether a task had *ever* failed
      instead of whether its *latest* attempt failed.
- [x] **M4: Graph revision and invalidation propagation.**
      `PROPOSE_GRAPH_REVISION` now runs `propagate_invalidation`. It
      invalidates the evidence of every downstream task that consumed the
      named evidence and supersedes those tasks' latest attempts, so they get
      re-verified. The commit and touched-paths staleness rule
      (`invalidation.py`) is built and tested against real git. Bug fixed:
      `_ready_tasks` had the same "ever vs. latest" mistake.
- [x] **M5: Codex CLI adapter and Docker sandbox.**
      `docker_sandbox.py` (launch, exec and idempotent stop),
      `codex_cli_adapter.py` (command construction and JSONL parsing) and
      `codex_execution_adapter.py`, which joins the two and always stops the
      container, even when the attempt fails. A Codex attempt refuses to run
      without a container.
- [x] **M6: Three-tier testing nodes, with EXECUTION dispatch wired live.**
      Adds `implement_exec`, `integration_test_write_exec`, `manual_test_exec`,
      `unit_verify_exec` and `integration_verify_exec`.
      `dispatch_execution_attempt` creates a real worktree, calls the runtime
      adapter, and derives `result_commit`/`changed_paths` from git.
      **Live-verified:** `scripts/live_smoke_test.py` reached
      `completed_verified` against a real `claude` session, with
      `unit_verify_exec` running in the worktree that `implement_exec`
      produced.
- [ ] **M7: Feature Blast end to end with a real Vercel deployment (in progress).**
      The first slice is a fixed chain, with no LLM-driven planning step yet:
      `implement_exec → unit_verify_exec → deploy_exec → manual_test_exec`.
      This milestone added `VercelDeployAdapter`, `deploy_exec`,
      `NodeType.output_strategy` (`manual_test_exec` reports pass/fail in
      `.forge_result.json` because no git diff can express it),
      `build_feature_blast_request_and_graph` and
      `scripts/live_feature_blast_m7.py`. Issues found in live runs and fixed:
      - A rejected Vercel token was retried 3 times. Runners can now flag a
        failure as unrecoverable (`requires_user_input`), which sends it
        straight to `REQUEST_USER_INPUT`.
      - Vercel returned `missing_project_settings` on a new project's first
        deploy. Every deploy now passes `skipAutoDetectionConfirmation=1`.
      - New projects default to Vercel Authentication, so the deployment
        served a login page. SSO protection is now disabled on the project
        after it is created.
      - `manual_test_exec`'s permission profile had no `Write` tool, and its
        narrow `Bash(<glob>)` entries denied compound commands. It now gets
        `Read`/`Write`/`Bash`, with `denied_bash_patterns` as the backstop.

      In the latest live run, `implement_exec`, `unit_verify_exec` and
      `deploy_exec` passed, and an independent probe of the deployed URL
      returned the expected response. **Remaining:** confirm a fully green
      run that includes `manual_test_exec`.
- [x] **M8: CLI polish, `forge trace` / `forge inspect`.**
      Adds a real `forge` console script (`[project.scripts]` in
      `pyproject.toml`): `forge trace <run_id>` replays a run's full audit
      trail chronologically (attempt dispatch, derived from
      `Attempt.started_at` since no separate dispatch `TraceEvent` exists,
      merged with every stored `TraceEvent` — results, decision outcomes,
      and which tasks a `PROPOSE_GRAPH_REVISION`'s invalidation propagation
      actually reached, cross-referenced from `Evidence.
      invalidated_by_evidence_id`); `forge inspect <run_id> [task_id]` gives
      a structural snapshot — run-level (tasks, statuses, which required
      gates are met) or, with `task_id`, one task's node type, status,
      every attempt, evidence, and the gates it depends on/satisfies. Task
      ids are only unique per graph, not globally, so `inspect` always
      takes a run id first rather than guessing from one bare id. All
      formatting/lookup logic is plain-text and lives in `forge/cli.py`
      (pure, returns strings); `scripts/forge_cli.py` is the thin,
      T20-exempt script that parses args and prints.

- [ ] **M9: Interactive TUI shell, per-project state, user-authored custom
      DAGs (non-TUI slice done; TUI phase 2 — dashboard/new-session/
      live-run/resume — also done; DAG view and visual builder still not
      started).** Scope settled via mockups and discussion. The first pass
      was everything buildable and fully testable as plain Python/CLI;
      this second pass is the actual Textual shell on top of it.
      Done (phase 1 — non-TUI engine wiring):
      - `Store.list_runs()` — every run, newest-updated first (sorted in
        Python, since `updated_at` lives inside the JSON blob rather than
        its own indexed column, unlike `graph_versions.version`).
      - Per-project `.forge/` discovery (`forge/project.py`):
        `find_repo_root`/`discover_project` resolve the enclosing git repo
        the same way `git` itself would, via `git rev-parse
        --show-toplevel` (same subprocess pattern as
        `worktree_manager.py`); `ensure_forge_dir` creates
        `.forge/forge.db` (schema-initialized via `db.connect`, not just
        touched) and `.forge/worktrees/` if missing, and adds `.forge/`
        to the repo's own `.gitignore` if nothing already covers it.
        `NotAGitRepoError` is the typed signal `scripts/forge_cli.py`
        catches to show the interactive "run `git init` here?" prompt —
        `forge/project.py` itself never prints or reads stdin.
      - `Mode.CUSTOM` + `Request.template_name`: one mode value for every
        user-authored DAG; `template_name` is what actually names which
        template produced a given request. No controller changes needed.
      - `forge/modes/custom_dag.py`: loads a `.forge/dags/*.json`
        template, substitutes `{{ input_name }}` placeholders in task and
        acceptance-criterion descriptions from a caller-supplied
        `dict[str, str]`, derives each `TaskSpec.node_kind` from the
        registry (the file never specifies it), defaults a task's missing
        `permission_profile` to its own node type's default, and raises a
        specific `CustomDagError` (naming the file and the exact bad
        value) for an unknown `node_type`/`permission_profile` rather
        than letting it fail deep inside `compile_and_validate` with a
        generic error. `depends_on` is the only dependency-authoring
        surface for now — no separate `context_only` edges section yet,
        a deliberate simplification. Never touches
        `deploy_authorized`/`Budgets` itself; both are taken exactly as
        the caller supplies them.
      - Three real commands on the `forge` console script: `forge run
        <objective> --journey "<text>" --deploy-authorized [--mode
        feature-blast|<custom-dag-name>] [--db PATH]`, `forge resume
        <run_id>`, `forge list`. All three resolve `.forge/` via
        `forge/project.py` unless `--db` overrides it, wire the real
        registries (`register_all`, `MechanicalRunnerRegistry`,
        `ExecutionHandlerRegistry`, `ModelRegistry`) and `run_sdk_node`,
        and drive `compile_and_validate` + `run_to_completion` the same
        way the live scripts already do. `forge run` refuses to start
        without `--deploy-authorized`. All three are tested end to end
        with a stubbed `ExecutionRunner` (the same fake-runner pattern
        `test_execution_dispatch.py` uses) against a real git repo and
        real worktrees — no real `claude` CLI or Vercel call anywhere in
        the suite.

      Done (phase 2 — the Textual shell itself, `forge/tui/*`):
      - `textual==8.2.8` added to `pyproject.toml`/`requirements.txt`
        (first dependency of its kind), plus `pytest-asyncio==1.4.0` in
        `requirements-dev.txt` to drive `tests/tui/*`'s `App.run_test()`
        (Pilot) tests. Bare `forge` (no subcommand — `scripts/forge_cli.py`
        now makes the subparser optional) hands off to
        `forge.tui.app.run_tui`.
      - `ForgeApp` (`forge/tui/app.py`): resolves `.forge/` on startup via
        the *exact same* `forge/project.py` discovery `forge run`/`resume`/
        `list` already use — `NotAGitRepoError` now surfaces as a real
        Textual modal (`ConfirmModal`, declining exits cleanly, nothing
        created) instead of a bare `input()`. Owns exactly one read-only
        `Store` every screen polls from, plus the `NodeTypeRegistry`/
        `PermissionProfileRegistry` needed for `compile_and_validate` and
        `build_inspect_report`. Defaults `self.theme = "textual-light"`
        (Textual 8.2.8 defaults to `"textual-dark"` if left untouched,
        confirmed empirically, not assumed) with a `t` binding to toggle
        light/dark. `start_new_run` builds the request/graph through the
        exact same `build_feature_blast_request_and_graph`/
        `build_custom_dag_request_and_graph` + `compile_and_validate` +
        `start_run` path `forge run` already uses — the one write the app
        does directly, on the main thread, over its own short-lived
        connection, before any background run exists. `open_run` starts a
        `RunWorker` (or reuses an already-running one) for anything
        ACTIVE/BLOCKED_AWAITING_INPUT, otherwise shows a static inspect
        summary — this is also exactly what resuming an orphaned run
        looks like, since `run_to_completion` reconciles orphaned attempts
        itself; no separate "resume" code path exists.
      - `RunWorker` (`forge/tui/runner.py`): one `threading.Thread` per
        live run, opening its *own* SQLite connection
        (`forge.controller.db.connect`, WAL mode) and driving the real
        `run_to_completion` — never a `sqlite3.Connection` shared across
        threads, never a second thing writing run state while it runs.
        Wires the real registries via `register_all` and the real
        `run_sdk_node` runner, same as `scripts/forge_cli.py`'s
        `_cmd_run`/`_cmd_resume`.
      - Four screens (`forge/tui/screens.py`), all reading the app's
        read-only `Store` and otherwise only calling back into `ForgeApp`:
        **Dashboard** (`Store.list_runs()` in a `DataTable`; empty state
        shows "no runs yet" and auto-advances to New Session; `n`/`r`/`q`,
        `enter` via `DataTable.RowSelected` since `DataTable` itself
        already claims the `enter` key). **New Session** (`OptionList` of
        Feature Blast + every `.forge/dags/*.json` found by the new
        `list_dag_templates`/`DagTemplateInfo` helper in
        `forge/modes/custom_dag.py`; a dynamically built `Input` form per
        mode's declared fields; an explicit deploy-authorization
        `ConfirmModal` that always defaults focus to "No"). **Live Run**
        (flat task table + a trace tail built by appending only the *new*
        lines of each poll's `build_trace_report` call, plus a status/
        elapsed header; polls every 0.75s; `d` pops back to the dashboard
        without touching the `RunWorker` thread at all — the run keeps
        going; `q` exits the whole app). **Run Summary** (a static,
        scrollable `build_inspect_report` for a terminal run, opened from
        the dashboard instead of the live view). Status text is colored
        via the active theme's own `$success`/`$warning`/`$error`
        variables (`App.get_css_variables()`), not hand-rolled hex codes,
        so it's correct in both themes.
      - Tests: `tests/tui/test_runner.py` (the thread/own-connection bridge
        directly, no Textual involved), `tests/tui/test_app.py` (seven
        `App.run_test()`/Pilot tests covering the git-init modal both ways,
        the empty-dashboard auto-advance, the deploy-confirm defaulting to
        "No", a full custom-DAG run through to `COMPLETED_VERIFIED` and
        back to the dashboard and a terminal-run summary, and the theme
        toggle), plus three new `list_dag_templates` tests in
        `tests/modes/test_custom_dag.py`. All against a real git repo and
        real worktrees with a stubbed `run_sdk_node`, same boundary the
        non-TUI suite already enforces — no real `claude` CLI/Vercel call.
        Also launched for real (pty/tmux aren't available in this sandbox,
        confirmed via `script`/`pty.fork()` both failing with permission
        errors; Textual's own headless driver — the same one
        `run_test()` uses, its officially supported no-pty path — is the
        closest substitute) end to end against a throwaway repo:
        dashboard → new session → custom-DAG form → deploy confirm → live
        run → `COMPLETED_VERIFIED` → dashboard → run summary → theme
        toggle, with an SVG screenshot saved at each step.

      **Remaining:** a topological DAG-layout function and the DAG view
      itself (zoom/zoom-in-with-pan, overview minimap — this phase is
      list-view-only, explicitly out of scope here), a visual
      keyboard-driven DAG builder that writes the same
      `.forge/dags/*.json` files `custom_dag.py` already loads, and
      `forge/cli.py` moving from returning strings to structured data so
      the TUI and plain-text commands share one data layer (the TUI
      currently reuses `forge/cli.py`'s string-building functions —
      `build_trace_report`/`build_inspect_report` — directly, plus two of
      its underlying private helpers, `_latest_attempts_by_task`/
      `_task_status`, rather than that refactor). Mid-run chat into a live
      EXECUTION session is still the biggest open risk in the whole
      milestone, sized separately — see Known gaps.

Everything after M9 (other execution-runtime adapters, modes beyond
custom-DAG templates) is backlog. See `requirements.md`.

## Known gaps

- A PreToolUse hook denying a tool call is covered by offline tests but
  hasn't been exercised in a live session yet.
- Automatic commit-based invalidation (`invalidation.py`) is not wired into
  the scheduler yet.
- The Docker/Codex CLI path is tested only by mocking `subprocess.run`. The
  JSONL event schema that `parse_codex_jsonl` relies on still needs checking
  against a real `codex exec --json` run. Live dispatch uses the Claude
  Agent SDK only for now.
- `integration_test_write_exec` and `integration_verify_exec` are registered
  but haven't been exercised end to end yet.
- M9 mid-run chat: confirmed buildable, not yet built. `claude-agent-sdk`
  0.2.165 already ships `ClaudeSDKClient` (persistent, bidirectional —
  `connect()` once, `query()` again for each follow-up, same
  `ClaudeAgentOptions`/hooks Forge already builds) alongside the one-shot
  `query()` function `sdk_execution_adapter.py`/`sdk_node_runner.py`
  actually use today. The real work: `ClaudeSDKClient` keeps an anyio
  task group alive for its whole `connect()`-to-`disconnect()` lifetime
  and refuses to be used across async contexts, but `run_sdk_node` opens
  a fresh event loop per attempt via `asyncio.run()` — switching adapters
  means giving a session its own long-lived loop (e.g. a background
  thread) with a thread-safe queue to deliver a chat message into it.
