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

- [x] **M9: Interactive TUI shell, per-project state, user-authored custom
      DAGs (non-TUI slice done; TUI phase 2 — dashboard/new-session/
      live-run/resume — also done; phase 3 — the DAG view on the live-run
      screen — also done; phase 4 — zoom/pan/minimap on that DAG view —
      also done; phase 5 — the visual DAG builder — also done; phase 6 —
      mid-run chat into a live EXECUTION session — also done; phase 7 —
      the CLI/TUI shared data layer — also done. M9 is complete.)** Scope
      settled via mockups and discussion. The first pass
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

      Done (phase 3 — the DAG view on the live-run screen):
      - `forge/tui/dag_layout.py`: `compute_dag_layout(tasks, edges) ->
        DagLayout`, a pure function with zero Textual imports. `rank` is
        the longest path from a root (a task with no incoming
        `"dependency"` edge) — this is what spreads a fan-out/fan-in
        shape like `implement_exec -> {unit_verify_exec,
        integration_test_write_exec -> integration_verify_exec} ->
        deploy_exec -> manual_test_exec` across ranks correctly (both
        fan-out branches land at the same rank; the fan-in task's rank is
        forced past whichever branch is longer). `lane` is a task's
        simple, deterministic position within its rank — the order tasks
        appear in `GraphVersion.tasks`, no crossing-minimization attempted.
        Computed once per `GraphVersion.id`, cached by `LiveRunScreen`
        rather than recomputed every poll tick. `DagLayoutError` guards
        against a cycle rather than assuming `compile_and_validate` always
        ran first, since this module stands on its own.
      - `forge/tui/dag_canvas.py`: `render_dag_canvas(tasks, edges,
        layout, statuses, style_for_status) -> rich.text.Text` — a 2D
        character-grid renderer (box-drawing/Unicode line characters, not
        SVG/pixels). Each task becomes a 3-row box (border/`● id`/border);
        edges route through the gap columns between ranks, with a stub,
        one vertical run, and a stub for a lane change, and otherwise a
        flat horizontal line. Every grid write only lands on a still-blank
        cell, so a multi-rank edge "flying over" an intervening rank's box
        just stops drawing across that box's footprint rather than
        corrupting it. Colors come from `style_for_status`, a callback the
        caller builds from the exact same `screens._status_style`/
        `App.get_css_variables()` the list view already uses — this
        module never picks its own colors.
      - `LiveRunScreen` (`forge/tui/screens.py`) now renders both a
        `#list-view` (the original table + trace tail) and a `#graph-view`
        (the new canvas, in a `VerticalScroll` so Textual's own scrolling
        covers a graph wider than the terminal — no zoom/pan built here,
        deliberately), toggled by a new `g` binding ("Graph view") off the
        *same* `refresh_run` poll tick — both are kept current every tick
        regardless of which is visible, so toggling never shows stale
        data.
      - Tests: `tests/tui/test_dag_layout.py` (linear chain, the exact
        fan-out/fan-in shape above, disconnected roots, `context_only`
        edges not affecting rank, cycle detection) and
        `tests/tui/test_dag_canvas.py` (box/border/glyph presence, a
        vertical run on a lane change, status style actually applied to
        a span, the empty-graph placeholder) — pure unit tests, no
        Textual. `tests/tui/test_app.py` adds two real `Pilot` tests: `g`
        toggling list/graph view mid-run and after completion on the
        existing two-task custom DAG, and a new fan-out-only custom DAG
        fixture (`tests/tui/conftest.py`'s `write_branching_dag` —
        `implement_exec -> {unit_verify_exec, integration_test_write_exec
        -> integration_verify_exec}`, no `deploy_exec`/network) run real
        end to end to confirm the canvas actually shows every task id
        after `COMPLETED_VERIFIED`. Also visually confirmed outside the
        test suite via Textual's headless-driver screenshot (same
        approach phase 2 used, since this sandbox has no real pty): the
        rendered SVG shows all four boxes, the fan-out connector, and the
        new `g Graph view` footer hint.

      Done (phase 4 — zoom, pan, and a minimap/overview on the DAG view):
      - `forge/tui/dag_canvas.py`'s `render_dag_canvas` gained a `detail:
        Literal["full", "compact"]` keyword (default `"full"`, matching
        phase 3's existing look exactly, so nothing else changed
        visually). "Zoom" on a character grid means level of detail, not
        pixel scaling: `"full"` is a 5-row box (border, id, `● status`,
        duration, border); `"compact"` is a 3-row box (border, `●
        id`, border) at roughly half the width — smaller per-node
        footprint, which is what actually lets more ranks fit across a
        terminal width. `durations` is a new optional `task_id -> str`
        map (`LiveRunScreen` derives it from each task's latest attempt's
        `started_at`/`finished_at`, "-" if it has neither) shown on
        `"full"`-detail boxes only. `dag_layout.py` itself is untouched —
        rank/lane stay purely topological; only `dag_canvas.py`'s own
        sizing constants (`_DetailConfig`, one per detail level) and
        per-box content differ. A new `canvas_size(layout, detail)` and
        `visible_rank_range(layout, detail, scroll_x, viewport_width)`
        are the pure geometry helpers the live-run screen's zoom-reset
        and minimap now depend on.
      - Pan is real: `#graph-view`'s own `VerticalScroll` had
        `overflow-x: hidden` by default (confirmed from Textual's own
        source, not assumed — `VerticalScroll`'s `DEFAULT_CSS`), which
        silently blocked horizontal scrolling even though the canvas was
        already wider than the viewport; `LiveRunScreen`'s own CSS now
        overrides it to `overflow-x: auto`. Arrow keys are wired as
        `priority=True` `Binding`s on `LiveRunScreen` itself (`action_
        pan_left/right/up/down`, `container.scroll_relative(..., animate=
        False, immediate=True, force=True)`), gated off by a `check_
        action` override so they're only live — and only shown in the
        footer — while the graph view is actually showing; when it isn't,
        `check_action` returns `False`, `run_action` reports unhandled,
        and the key falls through to whatever's actually focused (e.g.
        the task table's own row-cursor binding), confirmed by a real
        Pilot test that an "down" press still moves `DataTable.
        cursor_row` in list view. Priority bindings were necessary
        because a non-priority `Screen` binding loses to the focused
        widget's own same-key binding; checked from Textual's own
        `App._check_bindings`/`Binding.priority`, not assumed.
      - Zoom keybindings on `LiveRunScreen`, active only while the graph
        view is visible (same `check_action` gate as pan): `+`
        (`action_zoom_full`) and `-` (`action_zoom_compact`) switch
        `self._detail` and re-render immediately, without touching scroll
        position; `0` (`action_zoom_reset`) snaps `#graph-view`'s scroll
        back to `(0, 0)` *and* picks whichever detail level's
        `canvas_size` fits the container's current `container_size.width`
        — `"full"` if it fits, `"compact"` otherwise; if neither fits
        (a very wide graph), `"compact"` is still the better of the two,
        an acceptable terminal limitation rather than a bug to chase
        further. Footer hints match the existing `g`/`"Graph view"` style
        (`+`/`"More detail"`, `-`/`"Less detail (fit wider)"`,
        `0`/`"Reset zoom/pan"`).
      - `#graph-minimap`, a one-line `Static` below the graph view
        (shown/hidden together with it): `_update_minimap` reads
        `#graph-view`'s *actual* `scroll_x`/`scroll_y`/`container_size`
        every tick (plus right after a pan/zoom/toggle, deferred via
        `call_after_refresh` on the toggle since the container's size
        isn't updated yet in the same call stack as the `display` flip
        that revealed it) and renders a proportional `[▓▓▓··]`-style bar
        (`_minimap_bar`, a pure function) plus a `ranks X-Y/Z` readout
        from `visible_rank_range` — a terminal-appropriate stand-in for
        the mockup's pixel minimap, answering "where am I" from real
        numbers, never a static placeholder.
      - Tests: `tests/tui/test_dag_canvas.py` gained compact-vs-full
        detail assertions (narrower, drops the status word), a duration
        test, and `visible_rank_range` coverage (viewport math, clamping
        past the last rank, the empty-graph degenerate case).
        `tests/tui/test_app.py` adds four real `Pilot` tests on the
        fan-out custom DAG fixture, run with a deliberately narrow
        60-column terminal size so the graph genuinely needs to
        scroll/zoom to fit: zoom/pan actions disabled outside the graph
        view (and list-view arrow-key table navigation unaffected),
        `-`/`+` actually changing the rendered canvas's width,
        arrow-key panning changing `#graph-view.scroll_x` and `0`
        resetting it, and the minimap's text actually changing after a
        pan. Also visually confirmed outside the test suite the same way
        phase 2/3 did (Textual's headless-driver SVG screenshot, no real
        pty in this sandbox): on an 80-column terminal, the three-rank
        fan-out DAG's "full" detail needed 8 columns of horizontal
        scroll (`max_scroll_x == 8`) and the minimap bar visibly shifted
        after panning right; switching to "compact" detail dropped
        `max_scroll_x` to 0 (the whole graph fit) and the bar filled
        completely; `0` then reset scroll to 0 and chose `"compact"` on
        its own, since `"full"`'s width didn't fit that terminal.

      Done (phase 5 — the visual, keyboard-driven DAG builder):
      - `forge/tui/dag_builder_screen.py` (new module — `screens.py` was
        already 794 lines, and this needed its own set of modals):
        `DagBuilderScreen`, opened by a new `b`/"Build DAG" binding on
        `DashboardScreen` (the only change to an existing screen this
        phase made — no other binding or flow moved). Creates a *new*
        custom-DAG template only; loading an existing
        `.forge/dags/*.json` back in for further editing is explicitly
        out of scope for this pass, same as the task that specified it
        said to leave for later.
      - Reuses `forge/tui/dag_canvas.py`'s `render_dag_canvas` and
        `forge/tui/dag_layout.py`'s `compute_dag_layout`/
        `DagLayoutError` as-is for both rendering and cycle detection —
        no second rendering engine, no second cycle check. The renderer
        needed exactly one real addition: an optional
        `highlight_styles: Mapping[str, str] | None` that replaces a
        task's status-derived style for this render only, for the
        builder's "selected" / "connect source" / "connect target"
        cursor states (none of which are a run status). A task with no
        real run status yet renders with a placeholder `"authoring"`
        status string, which needed no further change at all — an
        unrecognized status already renders neutrally, same as
        `"not_started"` does today. Every task placed on the canvas is a
        real `TaskSpec` (and every dependency a real `EdgeSpec`), derived
        from the authored draft state the same way
        `forge/modes/custom_dag.py`'s own loader derives them — node
        type and kind are never free text, always the registry's.
      - Node-type palette: an `OptionList` populated from a new
        `NodeTypeRegistry.all_types()` (the one addition to that
        registry — nothing needed enumeration before this). Selecting an
        entry prompts for a unique task id and places it, selected.
      - Connect mode (`c` on a selected task, navigate with `]`/`[` to a
        second task, `enter` to confirm): appends the *source* task's id
        to the *target* task's `depends_on`. Cycle rejection reuses
        `dag_layout.compute_dag_layout` directly — the candidate edge is
        appended to a prospective edge list and handed to the exact same
        function the live-run view and `compile_and_validate` build on;
        a `DagLayoutError` means "would create a cycle," surfaced via a
        visible `app.notify(..., severity="error")`, never a silent
        no-op, and nothing is applied. `x` removes a task and strips its
        id from every other task's `depends_on` in the same step.
      - Template metadata (`m`: NAME/DESCRIPTION), inputs (`f`), and
        acceptance criteria (`v`, with `verification_method` chosen from
        an `OptionList` built from `typing.get_args` on
        `AcceptanceCriterion.model_fields["verification_method"]`
        itself, so it can never offer a value that model doesn't accept)
        are each a small, reused `_TextFieldModal` (validate-before-
        dismiss, so a bad entry re-prompts instead of handing the caller
        something to re-validate). The per-task inspector (`i`) shows
        `id`/node type/`depends_on` read-only and lets `description`/
        `acceptance_criterion_ids`/a `permission_profile` override be
        edited — `acceptance_criterion_ids` is checked against the
        criteria already declared above before the modal ever dismisses.
      - Save (`s`) builds a real `DagDefinition` (the exact pydantic
        model `forge/modes/custom_dag.py`'s loader parses) and writes
        `.forge/dags/<name>.json`. `permission_profile` overrides are
        validated with that module's own `resolve_permission_profile`
        (un-underscored from `_resolve_permission_profile` specifically
        so this screen could reuse it instead of re-implementing the
        same lookup/defaulting logic) — not a second check. After
        writing, the file is immediately re-parsed with
        `load_dag_definition` as a sanity check.
      - Tests (`tests/tui/test_dag_builder_screen.py`, real
        `App.run_test()`/Pilot): `b` reaching the builder from a fresh
        (auto-advances to New Session first, same as the empty-dashboard
        behavior phase 2 already tests) dashboard; a cycle attempt
        rejected with `a`'s `depends_on` left untouched and an "error"-
        severity notification containing "cycle"; and the actual
        acceptance criterion for this phase —
        `test_building_and_saving_a_branching_template_round_trips`
        places two tasks, connects them, declares an input and an
        acceptance criterion, attaches the criterion to the dependent
        task, saves, then feeds the saved file straight into
        `build_custom_dag_request_and_graph` + `compile_and_validate`
        and asserts a real, active `GraphVersion` comes back — not just
        that the file was written. Also visually confirmed outside the
        suite (Textual's headless-driver SVG screenshot, no real pty in
        this sandbox, same approach every prior TUI phase used): two
        connected boxes render correctly, and the connect-mode source
        task's text switches to the theme's own `warning` color (this
        theme's `accent` and `warning` happen to share one hex value,
        which is a property of the theme's palette, not of this code —
        both are still looked up by their own distinct semantic name).
      - This screen has no `set_interval`/poll loop at all, deliberately
        unlike `LiveRunScreen` — there is no live run and nothing else
        mutates the authored graph concurrently, so the canvas is just
        re-rendered synchronously right after whatever keypress or modal
        changed it. Run twice with no failures to check for the kind of
        timing issue two earlier phases hit.

      Done (phase 6 — mid-run chat into a live EXECUTION session):
      - `forge/adapters/execution/sdk_execution_adapter.py`'s
        `run_execution_attempt` swapped the one-shot `query()` function
        for a persistent `ClaudeSDKClient`: `connect(prompt)` opens the
        session (sending the initial message, same as `query()` did),
        `receive_response()` drains that turn's output the same way
        `query()`'s generator was drained before, `disconnect()` closes
        it — all still inside the one `asyncio.run()` call
        `sdk_node_runner.run_sdk_node` already wrapped this in; neither
        that call nor the scheduler's `dispatch_execution_attempt`
        changed at all. A new optional `chat_queue: queue.Queue[str] |
        None` parameter (plumbed through `run_sdk_node` too, keyword-only
        and `None` by default so every existing caller is unaffected) is
        what a chat message rides in on: when given,
        `_forward_chat_messages` runs concurrently with the receive loop
        and calls `client.query()` on that *same* client for every
        message it polls off the queue (a non-blocking `get_nowait()` on
        a 0.05s timer, specifically so it never blocks the receive loop
        itself) until an `asyncio.Event` signals the turn has finished. A
        failure inside that forwarding task (e.g. the transport already
        gone) is caught and dropped rather than allowed to replace the
        receive loop's own result — chat is additive, never a new way for
        the attempt itself to fail.
      - `forge/tui/runner.py`'s `RunWorker` gained `active_chat_queue:
        queue.Queue[str] | None`, non-`None` for exactly the duration of
        one EXECUTION attempt. `_run_sdk_node_with_chat` (now what's
        wired in as the scheduler's `execution_runner`, in place of
        `run_sdk_node` directly) creates a fresh queue, sets
        `active_chat_queue`, calls `run_sdk_node(..., chat_queue=...)`,
        and clears it back to `None` in a `finally` regardless of outcome.
        `run_to_completion` only ever calls `execution_runner` for an
        EXECUTION-kind task — `dispatch_mechanical_attempt` never
        references it at all — so being called through this wrapper *is*
        the "this attempt is chattable" signal; `deploy_exec` and every
        other MECHANICAL node needed no code change and never gets a
        queue. A message `put` after the queue has already gone back to
        `None` (the attempt finished) lands nowhere special: a plain
        `queue.Queue.put` never raises, it's just not read by anything.
      - `forge/tui/app.py`'s `ForgeApp.active_chat_queue_for(run_id)` is
        the one new read-only accessor `LiveRunScreen` calls — `None` when
        there's no `RunWorker` for that run at all, or its
        `active_chat_queue` is currently `None`.
      - `LiveRunScreen` (`forge/tui/screens.py`) gained `#chat-input`, an
        `Input` below both the list and graph views (always present,
        never hidden by the `g` toggle). Every 0.75s poll tick
        (`_refresh_chat_input`) re-derives its `disabled`/`placeholder`
        from `active_chat_queue_for` — enabled with an inviting
        placeholder while chattable, disabled with an explanatory one
        ("Chat not available right now...") otherwise, never silently
        accepting input with nowhere for it to go. `on_input_submitted`
        re-checks the queue at submit time too (defense against the
        message-arrives-right-as-the-attempt-finishes race): if it's
        still there, the message is `put` on it and the input is cleared;
        if not, the attempt is rejected with a visible warning notify and
        the typed text is left in place to retry, never cleared as if it
        had actually been sent.
      - Tests: `tests/adapters/execution/test_sdk_execution_adapter.py`
        gained a fake standing in for `ClaudeSDKClient` itself
        (connect/query/receive_response/disconnect, with a deliberately
        generous delay between response chunks so a concurrently-running
        chat-forwarding loop has a real window to act) — a baseline test
        proving the swap alone changes nothing observable, a test proving
        a queued message results in a real `.query()` call on the *same*
        client instance before the attempt completes, a test proving a
        message queued after the turn has already finished is never
        forwarded, and a test proving a forwarding-task failure doesn't
        replace the receive loop's own result.
        `tests/adapters/execution/test_sdk_node_runner.py` gained
        coverage that `chat_queue` is threaded straight through (and
        defaults to `None`). `tests/tui/test_runner.py` gained coverage
        that `active_chat_queue` is `None` before a run starts, is set
        for the EXECUTION task only (not the DAG's `unit_verify_exec`,
        proving a MECHANICAL attempt never gets one) and cleared once
        that attempt finishes, and that a message put on it from another
        thread while the attempt is genuinely still running reaches the
        (fake) runner blocked reading from it. `tests/tui/test_chat.py` is
        a real `Pilot`-driven end-to-end pass: the chat input starts
        disabled, becomes enabled once the worker thread's fake execution
        runner is actually blocked waiting on the queue, a typed-and-
        submitted message reaches that runner, the input disables again
        once the run completes, and a message submitted with no live
        attempt is rejected (text left in place, nothing queued) rather
        than silently dropped. Run twice with no failures, per this
        phase's own lesson about concurrency timing. Also visually
        confirmed outside the suite via Textual's headless-driver
        screenshot (no real pty in this sandbox, same approach every
        prior TUI phase used): `#chat-input` renders below the graph
        view, enabled with its live placeholder mid-attempt.

      Done (phase 7 — `forge/cli.py` becomes the one shared data layer):
      - `forge/cli.py` gained a set of frozen dataclasses
        (`RunListEntry`, `TaskStatusInfo`, `GateStatus`, `RunInspection`,
        `TraceEntry`, `RunAttemptsSummary`) and matching `compute_*`
        functions (`compute_run_list`, `compute_run_inspection`,
        `compute_trace_entries`, `compute_run_attempts_summary`) — pure
        readers over `Store`, no string formatting, no Textual imports.
        `build_trace_report`/`build_inspect_report`/
        `build_list_runs_report`/`build_run_attempts_summary` are now thin
        formatters over the matching `compute_*` result, producing the
        exact same plain-text output as before (verified against
        `tests/cli/test_cli.py`'s existing exact-string assertions,
        unchanged). `compute_run_inspection` is the one place
        `_task_status`/`_latest_attempts_by_task` (now private helpers
        used only inside `forge/cli.py` itself) derive a task's status
        from its latest attempt — the first duplication point this phase
        existed to remove.
      - `forge/tui/screens.py`: `DashboardScreen.refresh_runs` now calls
        `compute_run_list` instead of its own `Store.list_runs` +
        per-run `load_request` loop (the third independent query this
        phase's task called out). `LiveRunScreen.refresh_run` now calls
        `compute_run_inspection` directly for every task's status/latest
        attempt instead of importing `_task_status`/
        `_latest_attempts_by_task` from `forge/cli.py` — those two names
        are no longer imported anywhere outside `forge/cli.py`.
      - The second duplication point — `LiveRunScreen`'s trace tail —
        now calls `compute_trace_entries` and tracks how many *entries*
        (`self._trace_entries_written`, renamed from
        `_trace_lines_written`) it has already appended to `#trace-log`,
        rather than calling `build_trace_report` and diffing how many
        *lines* of its rendered text were already written. A multi-line
        entry (e.g. a graph revision's invalidated-evidence note) is
        still written in one `RichLog.write` call instead of two,
        rendering identically since `RichLog` already splits embedded
        newlines into separate visual lines. Checked specifically for
        this phase's own concurrency lesson: the first poll tick starts
        from `_trace_entries_written == 0` and renders every entry
        exactly once (confirmed by the existing `test_app.py` Pilot
        tests, which exercise several poll ticks over a real run), and
        `compute_trace_entries` is a pure read with no side effect, so
        calling it more than once (e.g. from a retried poll) can never
        double-render — only the append loop's own bookkeeping decides
        what's new.
      - No change to `forge inspect`/`forge trace`/`forge list`'s actual
        printed output — this was a structural refactor, not a content
        change. `tests/cli/test_cli.py` and
        `tests/cli/test_run_resume_list.py`'s exact-string assertions
        pass unmodified. Full suite (`pytest`, 312 tests) run twice,
        clean both times; `black`/`ruff`/`mypy --strict` all clean on
        `forge/`, `scripts/`, `tests/`.

- [x] **Post-M9: capture and expose the Claude Agent SDK session id.**
      A small, separate addition on top of M9 (not part of it), scoped
      explicitly to capture-and-expose only. Every EXECUTION attempt
      dispatched through the Claude Agent SDK gets a real session, with
      its own `session_id` carried on the terminal `ResultMessage`. Forge
      now captures it and persists it on the `Attempt`:
      - `ExecutionRunOutcome` (`forge/controller/execution_registry.py`)
        gained `session_id: str | None = None`.
      - `sdk_execution_adapter.run_execution_attempt`'s `ExecutionResult`
        gained the same field, filled by a new `_session_id_from_messages`
        helper that scans the already-collected `messages` list for a
        `ResultMessage` (imported from `claude_agent_sdk`) and reads its
        `session_id` — `None`, never a raised error, if no `ResultMessage`
        is present. `sdk_node_runner.run_sdk_node` threads it straight
        into the `ExecutionRunOutcome` it returns.
      - `Attempt` (`forge/models/attempt.py`) gained
        `sdk_session_id: str | None = None`; `scheduler.
        dispatch_execution_attempt` sets it from the outcome right after
        every EXECUTION attempt (retries included, since retries also go
        through this same function). Always `None` for a MECHANICAL
        attempt (`deploy_exec` etc. never call this path at all) and for
        a Codex CLI attempt (explicitly out of scope for this feature).
      - `forge/cli.py` needed no new dataclass field at all:
        `TaskStatusInfo.latest_attempt`/`RunInspection.attempts` already
        carry the full `Attempt`, so `sdk_session_id` was already in
        scope everywhere `compute_run_inspection` is read. Only
        `_attempt_lines` (used by `build_inspect_report`'s per-task
        detail, behind `forge inspect <run_id> <task_id>`) changed, to
        print `sdk_session_id: <value> (resume with: claude --resume
        <value>)` when one is present — nothing printed otherwise.
      - Checked whether the TUI would show this for free: it doesn't.
        `LiveRunScreen`'s task table only has TASK/NODE TYPE/STATUS/
        ATTEMPT columns, and `RunSummaryScreen` calls
        `build_inspect_report` with no `task_id` (the run-level report,
        which never descends into per-attempt detail) — neither renders
        a single task's attempt detail today. Showing the session id in
        the TUI would need real new UI (a column, or a per-task detail
        view) and was left undone, per this task's own explicit scope.
      - **Deliberately not done, a separate not-yet-decided feature:**
        automatically passing `resume=<sdk_session_id>` into a retry's
        `ClaudeAgentOptions` — no retry/resume policy changed at all.
        Forge only persists and displays the value; a person decides
        whether and how to use it themselves, via `claude --resume
        <sdk_session_id>` in their own terminal.
      - Tests: extended the fake `ClaudeSDKClient` double in
        `tests/adapters/execution/test_sdk_execution_adapter.py` (no
        second fake built) to yield a real `ResultMessage` with a
        `session_id`, proving it survives to `run_execution_attempt`'s
        return value, and that no `ResultMessage` at all means `None`
        rather than a raised error.
        `tests/adapters/execution/test_sdk_node_runner.py` gained
        coverage that the id is threaded through to
        `ExecutionRunOutcome`. `tests/controller/test_execution_dispatch.py`
        gained an end-to-end `run_to_completion` test (a real worktree,
        `implement_exec` + `unit_verify_exec`) proving the id lands on
        the EXECUTION attempt and stays `None` on the MECHANICAL one.
        `tests/cli/test_cli.py` gained two `build_inspect_report` tests —
        present-and-formatted-for-copy-paste, and silently absent when
        there isn't one. Full suite (`pytest`, 318 tests) run twice,
        clean both times; `black`/`ruff`/`mypy` (strict, via the
        project's own `files = ["forge", "scripts", "tests"]` config)
        all clean.

- [x] **Post-M9: restyle the TUI to match the original mockups.** A pure
      visual restyle of `forge/tui/*` — no screen's behavior, keybindings,
      or data flow changed. Addressed four gaps between the running app
      and the mockups it was designed from:
      - `forge/tui/chrome.py`: a new `Chrome` widget (three low-opacity
        status-colored dot glyphs, via each semantic color's own
        `-muted` theme token, then a muted `forge — <context>` title)
        used in place of `Header()` on every screen. Context per screen:
        the repo name on the dashboard (set in `on_mount`, once
        `ForgeApp.repo_root` is known — the constructor doesn't have it
        yet), "new session" on `NewSessionScreen`, the run id on
        `LiveRunScreen`/`RunSummaryScreen`, "build DAG" on
        `DagBuilderScreen`. Re-renders on `App.theme_changed_signal`
        (same subscribe pattern Textual's own `Footer` uses for
        `bindings_updated_signal`), so the dots/title stay correct
        across the `t` light/dark toggle.
      - `forge/tui/panel.py`: a new `LabeledPanel` (a bordered `Vertical`
        with a small uppercase label mounted as its first child) used
        for every logical section the mockups boxed separately:
        `DashboardScreen`'s run table (RUNS), `LiveRunScreen`'s task
        list/trace log/chat input/DAG canvas (TASKS/TRACE/CHAT/GRAPH),
        and `DagBuilderScreen`'s node-type palette/inspector/canvas
        (NODE TYPE PALETTE/INSPECTOR/CANVAS). A real Textual box-model
        gotcha surfaced building this: a label `Static` with `height: 1`
        plus its own `border-bottom` rendered as a blank line (the
        border claimed the only available row) — fixed by giving the
        label `height: 2`, confirmed with a headless-driver screenshot
        before and after.
      - `screens.py` gained `_status_dot_text` (a colored dot glyph
        *plus* the status word, e.g. `● PASSED`, sharing `_status_style`'s
        same style) replacing every bare `Text(status, style=...)` call
        site: the dashboard's run-status column, `LiveRunScreen`'s
        task-status column, and its run-header status. `dag_canvas.py`
        already drew status this way per-node and needed no change.
      - The stock `Footer` is restyled via CSS only (`ForgeApp.CSS`,
        targeting its own documented `footer-key--key`/
        `footer-key--description` component classes) so each binding
        reads as a themed `[key]` chip next to a muted description —
        no custom footer widget, so `Footer`'s real binding-discovery/
        `check_action`-driven show/hide behavior (e.g. `LiveRunScreen`'s
        graph-only actions) is untouched.
      - Tests: a new `tests/tui/test_chrome.py` (real `Pilot` runs)
        asserts the chrome dots/title and panel labels actually render,
        and that task/run/dashboard status cells read `● <status>`, not
        bare text. `tests/tui/test_app.py::test_g_toggles_between_list_
        and_graph_view` needed no assertion rewritten (it checks
        `#graph-view`'s own `display` flag, unaffected by the new
        `#graph-panel` wrapper added around it) — no existing test's
        exact-string assertion needed changing, since none of the
        touched status/run-list/task-table assertions in the existing
        suite checked literal rendered text (they check `Store`/`Run`
        state, not Textual's rendered output). Full suite (`pytest`, 323
        tests) run twice, clean both times; `black`/`ruff`/`mypy`
        (strict) all clean.

- [x] **Post-M9: fix what the restyle pass above missed.** That pass's own
      report verified against `build_inspect_report`/SVG *text content*,
      never an actually-rendered image — which is exactly why it still
      looked "old school" in real screenshots. This pass fixed five
      concretely diagnosed gaps, each confirmed by exporting a headless-
      driver SVG, converting it to PNG (`qlmanage -t`), and actually
      looking at it before and after, not just reading the code:
      - `forge/tui/app.py`: `ForgeApp.theme` now actually defaults to
        `catppuccin-latte` (toggling to `catppuccin-mocha` via `t`), not
        `textual-light`/`textual-dark` — Textual's own bland stock
        default, never an actual theme choice. Both names confirmed
        still registered in this Textual version via
        `App().available_themes` before relying on them.
      - `ForgeApp.CSS` gained `DataTable:focus > .datatable--cursor`,
        overriding the stock cursor look (confirmed from
        `DataTable.DEFAULT_CSS`: a full-opacity `$block-cursor-background`
        — `$primary` at 100%, bold text by default on most themes) with
        `background: $primary-muted` (a real token, confirmed from
        `textual.design.ColorSystem`: `primary.blend(background, 0.7)` —
        already the soft, accent-tinted blend this needed) and
        `text-style: none`. Both `DataTable`s also now construct with
        `cursor_foreground_priority="renderable"` (default is `"css"`),
        confirmed against `DataTable._get_styles_to_render_cell`'s real
        priority logic — without it, the CSS color would flatten a
        selected row's own red/green status color to one flat color,
        which is what a first pass at this looked like before actually
        screenshotting the selected row.
      - A real clipping bug, found and fixed: `LiveRunScreen`'s task
        table rendered its STATUS header as "ST". Root cause (confirmed
        empirically, not guessed): `add_column(..., width=None)` only
        grows a column to fit what's been measured so far, and when the
        sum of every auto-width column in a row exceeds the table's own
        viewport (each table lives inside a fractional-width
        `LabeledPanel`, not the whole terminal), `DataTable` doesn't wrap
        columns to fit — NODE TYPE's uncapped width (37 characters for
        `execution/integration_test_write_exec`) simply ate the space
        STATUS needed. Fixed in `forge/tui/screens.py` by giving every
        `DataTable` column an explicit width derived from the real
        enums/registry (`_status_column_width` over `AttemptStatus`/
        `RunStatus`, `_node_type_column_width` over
        `NodeTypeRegistry.all_types()`, `_RUN_ID_COLUMN_WIDTH`/
        `_UPDATED_COLUMN_WIDTH` from the real id/timestamp formats) and
        moving STATUS to right after each row's own identifying column
        (TASK, RUN ID) in both the live-run task table and the dashboard
        run table, so it's the one column that's virtually always still
        in view even when something else needs the table's own
        horizontal scroll. Confirmed with "not_started" (13 chars, the
        longest `AttemptStatus`/task-status value) and
        `completed_verified`/`failed_exhausted` (the longest `RunStatus`
        values) all rendering in full in a real screenshot.
      - `forge/tui/panel.py`'s `LabeledPanel` had no `background` set at
        all, so it inherited the Screen's own `$background` (see
        `textual.screen.Screen.DEFAULT_CSS`) — every panel was the exact
        same flat color as the page around it. Now `background: $surface`
        (Textual's own "what text usually goes on top of" token, one
        visible step up from `$background` on every registered theme,
        confirmed from each `Theme`'s real hex values rather than
        assumed) on the panel body, `background: $panel` (one step
        further, already used by the border and by `chrome.py`) on just
        the label strip — a visible card body plus a distinct header
        band, in both halves of the `t` toggle.
      - `screens.py::_status_style` dropped the `bold` it unconditionally
        added to every green/amber/red branch — since nearly every real
        status value matches one of those three sets, that made almost
        all data text in the app heavy (every status cell, the run-header
        line, the DAG canvas's per-node status line), which is what
        actually prompted the "too uniformly bold" look a rendered
        screenshot showed. Weight now stays reserved for genuine emphasis
        (`DataTable` headers, the chrome title's non-bold-but-intentional
        styling, panel labels) — color alone still carries the status
        semantic.
      - `dag_canvas.py` was checked, not touched: its box borders/status
        text already derive every color from the caller's
        `style_for_status`, so the new theme and the `_status_style`
        weight fix both flow through automatically (confirmed in the
        live-run graph-view screenshot) with no change to that module's
        own rendering logic.
      - Tests: `tests/tui/test_app.py::test_theme_defaults_light_and_
        toggles_dark` now asserts `catppuccin-latte`/`catppuccin-mocha`
        instead of `textual-light`/`textual-dark` — the actual new
        default, not a cosmetic rename. `tests/tui/test_chrome.py`'s two
        STATUS-column-index assertions moved from index 2/3 to index 1
        in both tables, since STATUS itself moved there — deliberate,
        not a blind "make it pass," since the column really did move.
        Full suite (`pytest`, 323 tests) run twice, clean both times;
        `black`/`ruff`/`mypy` (strict) all clean.

- [x] **Post-M9: effort-level customization, phase 1 of 4 (model/effort
      selection, wired through EXECUTION dispatch).** The first phase of a
      four-phase model/effort customization arc; the other three
      (custom-DAG per-task overrides, a live trace window into the running
      session, in-place interrupt-and-resume model/effort swapping) are
      discussed but not yet built.
      - Confirmed from `claude_agent_sdk.types` directly rather than a
        secondhand description: `EffortLevel = Literal["low", "medium",
        "high", "xhigh", "max"]`, and — the real finding here —
        `ClaudeAgentOptions.effort: EffortLevel | None` is its own
        top-level field, entirely separate from `ClaudeAgentOptions.
        thinking: ThinkingConfig | None` (which only ever controls
        adaptive/fixed-budget/disabled extended thinking; there is no
        `effort` key nested inside it). The initial plan going into this
        work assumed effort lived inside `thinking` — that assumption was
        wrong, confirmed by reading the real source, and the
        implementation below sets `effort=` directly, not `thinking=`.
      - `forge/models/common.py` gained Forge's own `EffortLevel` — the
        identical `Literal["low", "medium", "high", "xhigh", "max"]`, but
        defined here rather than imported from `claude_agent_sdk`, so
        `forge/models/*`/`forge/controller/*` still have no dependency on
        that package at all (only `forge/adapters/execution/*` does).
      - `NodeType` (`forge/controller/node_registry.py`) gained
        `default_effort`/`escalation_effort: EffortLevel | None = None`,
        mirroring `default_model`/`escalation_model`'s optionality exactly
        — with one deliberate difference: a node type is allowed to
        configure no effort at all (not an error, unlike having no
        `default_model`), in which case no `effort` override is set and
        the model's own default behavior applies.
      - `forge/controller/model_registry.py` gained `select_effort`, a
        separate function alongside `select_model` rather than one
        combined function/return type: the two policies share the exact
        same failure-count shape (both now built on a shared
        `_prior_failures` helper, extracted so they can't silently drift
        apart), but their "nothing configured" behavior differs in a way
        that's awkward to express as one return type — `select_model`
        must raise `NoDefaultModelConfiguredError` when unconfigured,
        while `select_effort` returning `None` for "unconfigured" is the
        normal, common case. Keeping them separate keeps each function's
        error semantics simple.
      - Set on every currently-registered EXECUTION node type
        (`forge/nodes/*.py`), each with its own stated reasoning:
        `implement_exec` (`default_effort="high"`,
        `escalation_effort="max"`) writes the actual feature code this
        project exists to produce — the highest-stakes, most open-ended
        work in the pipeline, warranting deep reasoning from the first
        attempt and the most expensive setting once that's demonstrably
        not enough. `integration_test_write_exec`
        (`default_effort="medium"`, `escalation_effort="high"`) needs real
        reasoning about how several merged pieces of work interact, but
        it's a narrower, better-scoped writing task than full feature
        implementation. `manual_test_exec` (`default_effort="low"`,
        `escalation_effort="medium"`) follows an already tightly specified
        recipe (run a Playwright script, take screenshots, report a
        verdict to a fixed file) — the smaller, more mechanical judgment
        call among this project's EXECUTION node types, so low effort
        suffices by default; escalation buys more careful observation, not
        deeper architectural reasoning this node never needed. MECHANICAL
        node types (`unit_verify_exec`, `integration_verify_exec`,
        `deploy_exec`) get no effort fields, same as they get no model
        fields today — they never call a model at all.
      - Threaded through dispatch, following the exact precedent
        `chat_queue` set in the mid-run-chat work, with one correction:
        that precedent turned out to be "add a keyword-only parameter to
        the concrete runner functions, not to the `ExecutionRunner` type
        itself" (`chat_queue` never touched `execution_registry.py` or
        `dispatch_execution_attempt`'s call signature at all — confirmed
        by re-checking that commit's actual diff). Effort can't follow
        that exact shape, because unlike chat it has to be *decided* by
        the scheduler (from `NodeType` + attempt history) and *handed to*
        whichever runner is configured — so `ExecutionRunner`
        (`forge/controller/execution_registry.py`) is now a `Protocol`
        with a keyword-only `effort: EffortLevel | None = None` parameter
        on its `__call__`, rather than the plain `Callable[[...], ...]`
        alias it was (which can't express a keyword-only parameter at
        all) — the same `Protocol`-for-an-injected-callable shape
        `DispatchRetryFn` in `decisions.py` already uses.
        `dispatch_execution_attempt` (`scheduler.py`) calls `select_effort`
        right alongside building the prompt and passes the result to
        `execution_runner(..., effort=effort)` on every dispatch (fresh
        attempts and decision-driven retries/escalations alike, since
        `select_effort` is recomputed from each dispatch's own attempt
        history). `run_sdk_node` (`sdk_node_runner.py`) and `RunWorker.
        _run_sdk_node_with_chat` (`forge/tui/runner.py`) both gained the
        same keyword-only `effort` parameter, forwarding it straight
        through — neither has any effort logic of its own.
      - `sdk_execution_adapter.py`'s `build_claude_agent_options` gained
        the same keyword-only `effort`, setting `ClaudeAgentOptions.
        effort=effort` directly (a trivial identity mapping, both sides
        being the same `Literal[...]` values) — `thinking` is untouched.
        `run_execution_attempt` forwards it through unchanged. With
        `effort=None` (no `default_effort` configured, true for every
        MECHANICAL node type and any EXECUTION one that doesn't set it),
        this is byte-for-byte what `ClaudeAgentOptions` construction
        looked like before this feature existed.
      - Tests: `TestSelectEffort` in `tests/controller/test_model_registry.py`
        mirrors `TestSelectModel`'s cases exactly (0/1 failures stay on
        default, 2+ escalate, other tasks'/non-FAILED attempts don't
        count, no escalation configured falls back to default) plus the
        one divergent case, no default configured at all returning `None`
        rather than raising. `tests/adapters/execution/
        test_sdk_execution_adapter.py` gained a test that `effort=` on
        `build_claude_agent_options` lands on `options.effort` (and leaves
        `options.thinking` untouched) and a test that no `effort` argument
        leaves it unset. `tests/adapters/execution/test_sdk_node_runner.py`
        gained the forwarding-through tests (default `None`, an explicit
        value) on the same pattern as the existing `chat_queue` ones.
        `tests/controller/test_execution_dispatch.py` gained
        `test_mechanical_attempt_never_reaches_an_effort_aware_execution_
        runner`: a capturing `execution_runner` fake proves it's called
        exactly once, for `implement_exec`, with its real registered
        `effort=="high"` — never for the MECHANICAL `unit_verify_exec` at
        all, which keeps `model_used == "mechanical"` same as before this
        feature existed. Every other `execution_runner` test fake across
        the suite (`tests/controller/test_execution_dispatch.py`,
        `tests/cli/test_run_resume_list.py`, `tests/tui/conftest.py`,
        `tests/tui/test_runner.py`, `tests/tui/test_chat.py`) gained the
        same keyword-only `effort` parameter `ExecutionRunner`'s new
        `Protocol` shape requires, mirroring how each already accepted
        `chat_queue`. Full suite (`pytest`, 336 tests) run twice, clean
        both times; `black`/`ruff`/`mypy` (strict) all clean.
      - **Not done, explicitly out of scope for this phase:** the Codex
        adapter (Codex-side effort support is a separate, later decision);
        custom-DAG per-task effort overrides (`forge/modes/custom_dag.py`,
        the next phase); the TUI's trace display; any interrupt/resume
        mechanism.

- [x] **Post-M9: effort-level customization, phase 2 of 4 (custom-DAG
      per-task model/effort overrides).** Lets a `.forge/dags/*.json`
      task override which model/effort it uses, following the exact
      `permission_profile`-override shape `forge/modes/custom_dag.py`
      already has: optional, validated at load time, defaulting to the
      node type's own behavior when omitted.
      - A real pre-existing gap phase 1 surfaced, confirmed again here and
        deliberately left alone (not this phase's job): a *fresh*
        EXECUTION dispatch's model never goes through `select_model`'s
        escalation policy at all — `scheduler.py` uses
        `model_used or task.model_override or node_type.default_model`
        directly. `select_effort`, by contrast, runs its real
        escalation-by-failure-count check on every dispatch. This
        asymmetry shaped the two overrides differently, below.
      - Example task entry with both overrides:
        ```json
        {
          "id": "implement",
          "node_type": "implement_exec",
          "description": "{{ objective }}",
          "model": "claude-opus-5",
          "effort": "low"
        }
        ```
        Both fields are optional; omitting either (or both) leaves
        `TaskSpec.model_override`/`effort_override` at `None`, identical
        to pre-phase-2 behavior.
      - `TaskSpec` (`forge/models/graph.py`) gained `model_override: str |
        None = None` and `effort_override: EffortLevel | None = None` —
        the carrier from graph-build time through to the scheduler, since
        `select_model`/`select_effort` only ever look at `NodeType`, never
        at a specific `TaskSpec` instance. `compile_and_validate`
        (`forge/controller/validator.py`) needed no change at all: it has
        no existing check on `default_model`/`escalation_model` either
        (model/effort were never part of its invariant set), `model_override`
        is already validated once at custom-DAG load time, and
        `effort_override` is structurally validated for free by its own
        `EffortLevel` `Literal` type the moment Pydantic constructs the
        `TaskSpec` — nothing left for the generic graph validator to check.
      - `forge/modes/custom_dag.py`: `DagTaskSpec` gained the same two
        optional fields (`model: str | None`, `effort: EffortLevel |
        None`), read straight off the JSON. A new `resolve_model_override`
        (same un-underscored, reusable-elsewhere shape as
        `resolve_permission_profile`) validates a given `model` against
        the real `ModelRegistry.validate_eligible` — unknown model or one
        not eligible for this task's `node_type` raises `CustomDagError`
        naming the file, task id, and bad value, same as an unknown
        `node_type`/`permission_profile` already does. `effort` needs no
        equivalent registry check — it's already constrained to a real
        `EffortLevel` by its own type the moment the file is parsed.
        `build_custom_dag_request_and_graph` gained a new required
        `model_registry: ModelRegistry` parameter (used only for this
        validation; model/effort *selection* stays entirely
        dispatch-time) — every call site updated (`forge/tui/app.py`,
        `scripts/forge_cli.py`, and every test that calls it).
      - **The escalation-interaction design decision this phase had to
        make explicit:** does a task-level `effort` override replace only
        the first rung (`default_effort`'s slot), with the node type's own
        `escalation_effort` still kicking in after repeated failures? Or
        does it fix effort permanently, disabling escalation entirely?
        Chose the former. `scheduler.py` gained `_effort_aware_node_type`:
        with no override, returns `node_type` unchanged; with one, returns
        a transient `node_type.model_copy(update={"default_effort":
        task.effort_override})` (`node_type` itself is never mutated) —
        `select_effort` then runs its real policy against *that*, so
        `escalation_effort` is completely untouched. Reasoning: a task
        author overriding the *starting* effort shouldn't also disable the
        safety net that exists precisely to recover from a starting point
        that turns out to be too weak — an override is a better first
        guess, not a promise that no failure will ever need more. Model
        has no equivalent interaction to decide, since a fresh dispatch's
        model never escalates at all today (the gap above) —
        `model_override` just slots into the existing
        `override-or-default` expression, with a decision-driven retry/
        escalation's explicit `model_used` still taking priority over it
        (unchanged: that's a later, more specific instruction than either
        the task's own override or the node type's default).
      - Tests: `tests/modes/test_custom_dag.py` gained
        `test_task_omitting_model_and_effort_leaves_both_overrides_unset`
        (no regression), `test_task_level_model_and_effort_overrides_land_
        on_the_task_spec`, `test_unknown_model_override_raises_specific_
        error_naming_file_and_value`, and `test_model_override_ineligible_
        for_this_node_type_raises_specific_error`. `tests/controller/
        test_execution_dispatch.py` gained
        `test_custom_dag_task_level_model_and_effort_overrides_reach_the_
        execution_runner` (a real custom-DAG JSON file, loaded, compiled,
        and run through the real scheduler with a capturing
        `execution_runner` — not just a Pydantic round-trip) and
        `test_effort_override_replaces_only_the_default_rung_escalation_
        still_applies` (the chosen interaction rule, proven end to end: a
        task overriding `implement_exec`'s effort to `"low"` runs its
        first two attempts at `"low"`, not `implement_exec`'s own
        `"high"`, and its third — after 2 prior `FAILED` attempts —
        escalates to `implement_exec`'s own `escalation_effort`
        (`"max"`), never back to `"low"` and never a value the override
        invented). Full suite (`pytest`, 342 tests) run twice, clean both
        times; `black`/`ruff`/`mypy` (strict) all clean.
      - **Not done, explicitly out of scope for this phase:** the Codex
        adapter; the TUI's trace display (next phase); any interrupt/
        resume mechanism (phase 4); fixing the pre-existing
        fresh-dispatch model-escalation gap noted above — this phase works
        around it consistently (`model_override` slots into the same
        `override-or-default` expression), it doesn't fix it.
- [x] **Follow-up: a real `ModelRegistry`.** Phase 2 flagged that every live
      entry point (`forge/tui/app.py`, `forge/tui/runner.py`,
      `scripts/forge_cli.py`) constructed `ModelRegistry([])` — meaning a
      custom-DAG task's `model` override, however correctly validated in
      tests, would always fail eligibility in actual use, since no model was
      ever registered as eligible for anything. `forge/controller/
      model_registry.py` gained `build_default_model_registry()`,
      registering `claude-sonnet-5` (1M context, $2/$10 per MTok, eligible
      for all three EXECUTION node types plus the one registered decision
      node type, `retry_or_escalate_policy`), `claude-opus-5` ($5/$25,
      the three EXECUTION node types), and `claude-haiku-4-5` (200K
      context, $1/$5, same three — not currently any node type's default,
      but now a real option a custom-DAG override or future config can
      reach). Pricing/context figures came from Anthropic's own current
      published rates, not a guess. `ModelRegistry` keys purely by
      `model_id` (not `(model_id, adapter_kind)`), so `claude-sonnet-5` gets
      one entry covering both its execution and decision uses — documented
      in the function's own docstring, along with the finding that
      `adapter_kind` is pure metadata nowhere else in the codebase reads.
      All four call sites now share this one function. Proven end to end
      (not just toolchain-clean): `ModelRegistry.validate_eligible` now
      actually succeeds for `claude-opus-5`/`implement_exec` and
      `claude-sonnet-5`/`retry_or_escalate_policy`, and still correctly
      rejects a model for a MECHANICAL node type. Full suite (342 tests)
      run twice, clean both times; `black`/`ruff`/`mypy` (strict) clean.

Everything after M9 is backlog. See `requirements.md`.

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
