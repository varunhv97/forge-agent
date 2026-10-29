# Architecture

A quick map of the system. See `requirements.md` for scope and `tasks.md`
for progress.

## Three artifacts, one authority

- **Logical Task DAG** (`GraphVersion`) — the versioned plan. Append-only
  versions, immutable once active.
- **Execution Attempt DAG** (`Attempt` rows) — immutable history of what
  actually ran. Retries/escalations are new forward-pointing rows, never
  back-edges.
- **Controller state machine** — the only thing that reads both and writes
  transitions. Lives in SQLite (`forge/controller/db.py`, WAL mode, explicit
  transactions).

Nodes produce typed proposals (`DecisionProposal`, `NodeResult`). Only
`forge/controller/*` applies them.

## Modules

```
forge/
  cli.py          The one data layer `forge trace`/`inspect`/`list`/
                  `run`/`resume` and the TUI both read from. `compute_run_list`/
                  `compute_run_inspection`/`compute_trace_entries`/
                  `compute_run_attempts_summary` are pure readers over
                  `Store` returning frozen dataclasses (`RunListEntry`,
                  `RunInspection`/`TaskStatusInfo`/`GateStatus`,
                  `TraceEntry`, `RunAttemptsSummary`) — no string
                  formatting, no Textual imports; `compute_run_inspection`
                  is the single place a task's status is derived from its
                  latest attempt. `build_trace_report`/`build_inspect_report`/
                  `build_list_runs_report`/`build_run_attempts_summary` are
                  thin plain-text formatters over the matching `compute_*`
                  result, behind `forge trace`/`inspect`/`list`/`run`/
                  `resume`; the T20-exempt printing/argparse/interactive
                  prompts live in scripts/forge_cli.py (the console_scripts
                  entry point)
  project.py      per-project `.forge/` discovery: find_repo_root/
                  discover_project resolve the enclosing git repo (same
                  subprocess pattern as isolation/worktree_manager.py) and
                  ensure_forge_dir creates `.forge/forge.db` +
                  `.forge/worktrees/` and a `.gitignore` entry if missing.
                  NotAGitRepoError is the typed signal scripts/forge_cli.py
                  catches to show the interactive `git init` prompt — this
                  module itself never prints or reads stdin
  models/        typed contracts (Pydantic): common, request, graph,
                  attempt, evidence, decision, permissions, model_registry,
                  run, trace. common.py also carries Forge's own
                  EffortLevel (Literal["low", "medium", "high", "xhigh",
                  "max"]) — structurally identical to the Claude Agent
                  SDK's own type of the same name, but defined here so
                  forge/models/*  and forge/controller/* stay free of any
                  dependency on the SDK package; only
                  forge/adapters/execution/* translates it into the real
                  SDK's own types. attempt.py's AttemptStatus gained
                  INTERRUPTED_FOR_SWAP (post-M9 phase 4) — neither PASSED
                  nor FAILED, for an attempt cut short by a person's own
                  model/effort swap request rather than a pass/fail
                  verdict; Attempt itself gained pending_resume_model/
                  pending_resume_effort (sdk_session_id, already existing,
                  is reused as the session to resume from) — see "In-place
                  model/effort swap, concretely" below
  controller/
    validator.py        compile_and_validate() — acyclicity + domain
                          invariants, same path for templates and revisions
    node_registry.py     global NodeTypeRegistry, shared across modes;
                          also NodeTypeRegistry.all_types() — every
                          registered node type, in registration order,
                          added for the TUI DAG builder's node-type
                          palette (nothing needed enumeration before)
    db.py                SQLite connection + schema + explicit transactions
    store.py             typed CRUD over the schema above
    mechanical.py        MechanicalRunnerRegistry — deterministic checks,
                          no LLM, no adapter
    model_registry.py    ModelRegistry (escalation eligibility) +
                          select_model/select_effort — same failure-count
                          policy (shared _prior_failures helper), kept as
                          two functions since "no default configured" means
                          something different for each (an error for model,
                          a normal "don't override" for effort)
    decisions.py          apply_decision — the only place a DecisionProposal
                          becomes a real state change (see below)
    decision_dispatch.py  invoke_decision_node + deterministic fallbacks +
                          the fixed decision_node_type->model_id table
    invalidation.py       evidence_is_stale — the commit/touched-paths
                          staleness rule, real-git-tested; not yet wired
                          into live dispatch (see Known gap below)
    execution_registry.py ExecutionHandlerRegistry — the EXECUTION-node
                          counterpart to MechanicalRunnerRegistry: maps a
                          node type to its build_input/build_prompt pair.
                          ExecutionRunner is a Protocol (not a plain
                          Callable[[...], ...] alias — that shape can't
                          express a keyword-only parameter at all), same
                          as DispatchRetryFn in decisions.py: __call__(model,
                          prompt, profile, worktree_path, *, effort:
                          EffortLevel | None = None, resume_session_id:
                          str | None = None) -> ExecutionRunOutcome.
                          EffortSwapRequest (post-M9 phase 4, a frozen
                          dataclass: effort: EffortLevel, model: str |
                          None = None) is the validated, in-flight request
                          that rides RunWorker.effort_swap_queue and comes
                          back out on ExecutionRunOutcome.swap_requested —
                          see "In-place model/effort swap, concretely"
                          below
    attempt_commit.py     current_head/commit_dirty_changes/changed_paths_
                          between — mechanically derives what an EXECUTION
                          attempt actually did, from git, never from what
                          the node claims
    scheduler.py          controller_tick / run_to_completion / reconcile;
                          dispatch_mechanical_attempt and
                          dispatch_execution_attempt (real worktree, real
                          prompt, real adapter call); failed tasks of
                          either kind get driven through a decision each
                          tick instead of staying failed forever.
                          dispatch_execution_attempt also looks up this
                          task's immediately prior attempt before dispatch:
                          if it's AttemptStatus.INTERRUPTED_FOR_SWAP (post-M9
                          phase 4), this one dispatch resumes that session
                          and honors its pending_resume_model/
                          pending_resume_effort, bypassing the normal
                          model/effort selection entirely — see below
  nodes/          implement_exec, integration_test_write_exec,
                  manual_test_exec (EXECUTION, output_strategy=
                  "result_file" — writes .forge_result.json, no git diff to
                  judge pass/fail from); unit_verify_exec,
                  integration_verify_exec, deploy_exec (MECHANICAL,
                  worktree-aware; deploy_exec reaches the real network);
                  register_all() populates every registry at once,
                  including deploy_exec's runner (make_deploy_runner) and
                  manual_test_exec's build_input adapter
                  (_manual_test_build_input, resolves target_base_url from
                  the upstream deploy_exec output)
  modes/          feature_blast.py — build_feature_blast_request_and_graph,
                  the descoped M7 slice's Request+draft GraphVersion
                  builder; the caller still runs the draft through
                  compile_and_validate itself, same as any other graph.
                  custom_dag.py — build_custom_dag_request_and_graph loads
                  a `.forge/dags/*.json` template (Mode.CUSTOM,
                  Request.template_name names which one), validates every
                  task's node_type/permission_profile/model against the
                  real registries (CustomDagError names the file and bad
                  value on failure), substitutes `{{ input }}`
                  placeholders, and hands back the same kind of draft for
                  the caller to run through compile_and_validate — never
                  touches deploy_authorized/Budgets itself, both are
                  caller-supplied. DagTaskSpec's per-task fields:
                  node_type/description/depends_on/acceptance_criterion_ids
                  are required; permission_profile/model/effort are each
                  optional overrides, None by default. permission_profile
                  defaults to the node type's own NodeType.
                  permission_profile (resolve_permission_profile — not
                  underscore-prefixed, on purpose, since it's reused
                  directly by the TUI's DAG builder,
                  forge/tui/dag_builder_screen.py, to validate the same
                  override at template-save time). model, when given, is
                  checked by resolve_model_override against the real
                  ModelRegistry.validate_eligible (same CustomDagError
                  convention) and carried onto TaskSpec.model_override;
                  effort is carried onto TaskSpec.effort_override with no
                  extra registry check (its own EffortLevel Literal type
                  already constrains it at parse time). Both overrides are
                  None (no override, identical to pre-phase-2 behavior)
                  whenever the task's JSON omits them — see "Model and
                  effort selection, concretely" below for how
                  dispatch_execution_attempt actually consumes them.
                  build_custom_dag_request_and_graph takes a
                  model_registry: ModelRegistry parameter for this
                  validation only — selection stays dispatch-time.
  adapters/
    execution/    sdk_execution_adapter.py — wires forge/isolation/hooks.py
                  into real claude_agent_sdk types (ClaudeAgentOptions,
                  HookMatcher); run_execution_attempt drives one attempt
                  through a persistent ClaudeSDKClient (connect/
                  receive_response/disconnect), not the one-shot query()
                  function, specifically so a chat message can be
                  injected mid-session (M9 phase 6) — an optional
                  chat_queue: queue.Queue[str] | None runs
                  _forward_chat_messages concurrently with the receive
                  loop (one asyncio.Event signals when the turn's own
                  response has finished, which also stops the forwarding
                  loop), calling client.query() on that same client for
                  every message polled off the queue; with no queue at
                  all this is a plain ClaudeSDKClient turn with nothing
                  injected, the observably-identical baseline the swap
                  was proven against first. An optional live_messages:
                  queue.Queue[Any] | None (post-M9 phase 3) gets every
                  message put_nowait'd onto it the moment it's appended to
                  messages — the live-trace counterpart to chat_queue, same
                  cross-thread-queue.Queue shape, opposite direction; see
                  "Live trace window, concretely" below. render_live_message
                  (same module, so only here does forge/tui/* reach a real
                  claude_agent_sdk type at all) turns one message into the
                  short, abbreviated lines that window shows.
                  build_claude_agent_options also
                  takes an optional effort: EffortLevel | None, set
                  straight onto the real ClaudeAgentOptions.effort field —
                  confirmed from claude_agent_sdk.types directly that this
                  is its own top-level field, not a key nested inside
                  ClaudeAgentOptions.thinking (an initial assumption going
                  into this feature, corrected by reading the real source
                  before writing any code against it); also takes an
                  optional resume: str | None (post-M9 phase 4), set onto
                  ClaudeAgentOptions.resume — confirmed independent of
                  model/effort from the same source. run_execution_attempt
                  also takes optional model_swap_queue: queue.Queue[str] |
                  None / effort_swap_queue: queue.Queue[EffortSwapRequest]
                  | None (post-M9 phase 4): _watch_model_swap polls the
                  former and calls client.set_model() in place;
                  _watch_effort_swap polls the latter and calls
                  client.interrupt(), letting the receive loop end on its
                  own (a confirmed-from-source behavior, with a bounded
                  timeout fallback that force-cancels the receive task if
                  it doesn't) — see "In-place model/effort swap,
                  concretely" below for the full mechanism.
                  sdk_node_runner.py
                  bridges it to the scheduler's sync ExecutionRunner
                  contract (asyncio.run, same pattern as the decision
                  adapter's bridge), threading chat_queue, effort,
                  live_messages, model_swap_queue, effort_swap_queue, and
                  resume_session_id straight through (all keyword-only,
                  None by default);
                  codex_cli_adapter.py
                  (command construction + JSONL parsing, decoupled from
                  the sandbox via a duck-typed executor);
                  codex_execution_adapter.py (wires the Codex adapter to
                  docker_sandbox, owns the launch/exec/stop lifecycle —
                  stop always runs, even on failure)
    decision/     anthropic_api_decision_adapter.py — direct Anthropic
                  Messages API calls with forced tool-use structured output
    deploy/       vercel_adapter.py — VercelDeployAdapter: creates a
                  deployment via the Vercel REST API and polls it to a
                  terminal readyState (READY/ERROR), real httpx.Client,
                  fully offline-tested via httpx.MockTransport. Every
                  create-deployment call passes
                  skipAutoDetectionConfirmation=1, since deploy_exec always
                  deploys a fresh, disposable project name — every
                  deployment is that project's first, and Vercel requires
                  either that flag or an explicit projectSettings object
                  for a project with no prior framework-detection answer
                  on file. The Vercel token itself must be Full-Account
                  scoped, not Team/Project scoped — a narrower scope can't
                  create new projects and Vercel rejects it as
                  invalidToken, which VercelAuthenticationError surfaces.
                  deploy() also PATCHes the new project right after
                  creating it (ssoProtection: null) — new Vercel projects
                  default to Vercel Authentication on every deployment,
                  which would otherwise return a login-page redirect to
                  any unauthenticated request instead of the app's actual
                  response, defeating the point of deploying something
                  Forge itself (or a human without a Vercel login) can
                  reach
  isolation/
    worktree_manager.py  create_worktree/remove_worktree over real git
                          worktrees, idempotent cleanup
    hooks.py              the PreToolUse policy decision engine itself
                          (ToolCallEvent -> HookDecision) — zero dependency
                          on any specific agent-runtime SDK by design
    docker_sandbox.py     launch_container/exec_in_container/stop_container
                          — the enforcement substitute for adapters with no
                          hook mechanism (Codex CLI today)
  tui/            the interactive shell launched by bare `forge` (no
                  subcommand — scripts/forge_cli.py makes the subparser
                  optional and hands off to run_tui). UI layer only: reads
                  a Store, never writes run state itself except the one
                  start_new_run call below.
    app.py          ForgeApp — resolves .forge/ via the same
                    forge/project.py discovery forge run/resume/list use
                    (NotAGitRepoError surfaces as a real ConfirmModal, not
                    input()); owns one read-only Store every screen polls;
                    theme defaults to "catppuccin-latte" (confirmed
                    registered via App().available_themes; Textual 8.2.8
                    defaults to "textual-dark" otherwise), `t` toggles it
                    to "catppuccin-mocha" — not the stock
                    textual-light/textual-dark pair a first restyle pass
                    left in place (Textual's own bland default, not an
                    actual theme choice; fixed in the restyle follow-up,
                    see tasks.md). ForgeApp.CSS also restyles every
                    DataTable's focused row-cursor (`$primary-muted`, a
                    real soft accent-tinted blend token, `text-style:
                    none`) in place of the stock full-opacity solid
                    `$block-cursor-background` block.
                    start_new_run builds the request/graph through the
                    same build_feature_blast_request_and_graph/
                    build_custom_dag_request_and_graph +
                    compile_and_validate + start_run path forge run
                    already uses — the one write this layer does
                    directly, on the main thread, before any
                    RunWorker for that run exists. open_run starts (or
                    reuses) a RunWorker for anything ACTIVE/
                    BLOCKED_AWAITING_INPUT, otherwise shows a static
                    inspect summary — also exactly what resuming an
                    orphaned run looks like, since run_to_completion
                    reconciles orphaned attempts itself.
                    active_chat_queue_for(run_id) (M9 phase 6) is the one
                    new read-only accessor LiveRunScreen's chat input
                    polls: None when there's no RunWorker for that run,
                    or its own active_chat_queue is currently None.
                    live_messages_for(run_id) (post-M9 phase 3) is the same
                    shape for the live trace panel's own queue.
                    request_execution_swap(run_id, node_type_name, *,
                    model, effort) (post-M9 phase 4) is the one call
                    LiveRunScreen's swap modal makes — delegates to
                    forge.controller.decisions.request_execution_swap (the
                    validated path) with whichever of the live RunWorker's
                    model_swap_queue/effort_swap_queue currently exist
                    (None/None when there's no RunWorker for that run,
                    same as the two accessors above); never pokes a queue
                    directly itself.
    runner.py       RunWorker — one threading.Thread per live run, its
                    own SQLite connection (db.connect, WAL mode), driving
                    the real run_to_completion with the real registries.
                    Never a sqlite3.Connection shared across threads; the
                    only writer of run state once a run is live, same as
                    forge run/resume already are.
                    active_chat_queue: queue.Queue[str] | None (M9 phase
                    6) is non-None for exactly the duration of one
                    EXECUTION attempt. _run_sdk_node_with_chat — wired in
                    as the scheduler's execution_runner in place of
                    run_sdk_node directly — creates a fresh queue per
                    attempt, sets active_chat_queue, calls run_sdk_node(
                    ..., chat_queue=...), and clears it back to None in a
                    finally regardless of outcome. run_to_completion only
                    ever calls execution_runner for an EXECUTION-kind
                    task (dispatch_mechanical_attempt never references it
                    at all), so being called through this wrapper *is*
                    the "this attempt is chattable" signal — deploy_exec
                    and every other MECHANICAL node needed no change and
                    never gets a queue. A message put after the queue has
                    already gone back to None lands nowhere: a plain
                    queue.Queue.put never raises, it's just unread.
                    live_messages: queue.Queue[Any] | None (post-M9 phase
                    3) is set/cleared by that same _run_sdk_node_with_chat
                    call, same finally, literally the chat_queue pattern
                    copied for the opposite direction — see "Live trace
                    window, concretely" below for the unbounded-queue and
                    stranded-message reasoning.
                    model_swap_queue: queue.Queue[str] | None /
                    effort_swap_queue: queue.Queue[EffortSwapRequest] |
                    None (post-M9 phase 4) are the same lifecycle again —
                    set/cleared by the same _run_sdk_node_with_chat call,
                    same finally — now forwarded into run_sdk_node's own
                    model_swap_queue/effort_swap_queue/resume_session_id
                    parameters (the last threaded straight through from
                    whatever the scheduler passed dispatch_execution_attempt
                    on this call, see "In-place model/effort swap,
                    concretely" below). Only forge.controller.decisions.
                    request_execution_swap (via ForgeApp.
                    request_execution_swap) ever puts onto either queue.
    chrome.py       Chrome — the three-dot-plus-title bar used in place
                    of Header() on every screen (post-M9 restyle to match
                    the original mockups). Dots use each semantic color's
                    own `-muted` theme token (real hex, e.g. `error-muted`)
                    for "reduced opacity"; the title is left unstyled and
                    instead inherits this widget's own `color: $text-muted`
                    CSS, since that variable's real value (`"auto 60%"`)
                    is a Textual-CSS-only directive that crashes Rich's
                    own style parser if passed as a per-span Text style
                    directly — confirmed empirically (MissingStyle), not
                    assumed. Re-renders on App.theme_changed_signal.
    panel.py        LabeledPanel — a bordered Vertical with a small
                    uppercase label Static mounted as its first child
                    (post-M9 restyle): the TASKS/TRACE/CHAT/GRAPH/RUNS-
                    style boxed sections the mockups used, in place of
                    flat sibling widgets with at most a single divider
                    border. Purely visual; callers still query_one
                    whatever they mount inside it by that widget's own
                    id. The label needs `height: 2`, not 1 — a Textual
                    box-model gotcha found building this: with `height: 1`
                    plus the label's own `border-bottom`, the border
                    claimed the row and the text never painted.
                    `background: $surface` on the panel itself (restyle
                    follow-up — it had none before, so every panel just
                    inherited the Screen's own `$background` and read as
                    flat, not a card) plus `background: $panel` on just
                    the label strip, one step further, for a visible
                    header band on top of the card body.
    screens.py      DashboardScreen/NewSessionScreen/LiveRunScreen/
                    RunSummaryScreen/ConfirmModal. DashboardScreen's `b`
                    binding ("Build DAG") pushes dag_builder_screen.py's
                    DagBuilderScreen — the only change this module made
                    for the visual DAG builder; everything else about it
                    lives in that new module. DashboardScreen.refresh_runs
                    and LiveRunScreen.refresh_run call forge/cli.py's
                    compute_run_list/compute_run_inspection directly for
                    run-list rows and per-task status/latest-attempt data
                    (M9 phase 7 — the only place that derivation happens
                    now); RunSummaryScreen still reuses
                    build_inspect_report's plain text as-is for a
                    terminal run's static summary. Status text is colored
                    via the active theme's own
                    $success/$warning/$error variables, never hand-rolled
                    hex, and (post-M9 restyle) rendered as a colored dot
                    glyph plus the status word via the new
                    _status_dot_text helper, not bare colored text, at
                    every status call site (dashboard run-status column,
                    LiveRunScreen task-status column and run-header).
                    _status_style no longer forces bold on every colored
                    branch (restyle follow-up — nearly every real status
                    value is colored, so that made almost all data text
                    heavy; color alone still carries the semantic).
                    DataTable column widths are explicit, derived from
                    the real AttemptStatus/RunStatus enums and
                    NodeTypeRegistry.all_types() (_status_column_width/
                    _node_type_column_width), not left on "auto" — an
                    auto-width column only grows to fit what's been
                    measured so far, and when a row's columns together
                    exceed the table's own fractional-width panel,
                    later columns (STATUS, specifically) could render
                    clipped or scroll out of view entirely; STATUS is
                    also now the column right after each table's own
                    identifying column (TASK, RUN ID) for the same
                    reason. Both DataTables construct with
                    cursor_foreground_priority="renderable" so a
                    selected row's own status color survives under the
                    cursor instead of flattening to one CSS color.
                    Every screen's Header()/Footer siblings are now
                    chrome.py's Chrome plus panel.py's LabeledPanel
                    around the task list/trace log/chat input/DAG canvas
                    (dag_builder_screen.py similarly for its palette/
                    inspector/canvas) — restyle only, no behavior change.
                    LiveRunScreen renders two views of the same poll
                    tick's data, toggled with `g`: the original flat task
                    table + trace tail, and a DAG canvas (#graph-view).
                    #graph-view overrides VerticalScroll's own default
                    `overflow-x: hidden` to `auto` (confirmed from
                    Textual's source — a bare VerticalScroll does not
                    scroll horizontally on its own), and LiveRunScreen
                    binds arrow keys as priority Bindings
                    (action_pan_left/right/up/down, scroll_relative with
                    force=True) so pan is actually reachable from the
                    keyboard, not just a scrollbar. `+`/`-` toggle
                    render_dag_canvas's detail level ("full"/"compact");
                    `0` snaps scroll back to the origin and picks
                    whichever detail's canvas_size fits the current
                    container_size.width. All four pan actions plus the
                    three zoom actions are gated by a check_action
                    override so they're only active (and only shown in
                    the footer) while the graph view is visible — outside
                    it, check_action returns False and the key falls
                    through to whatever's actually focused (e.g. the task
                    table's own row-cursor binding). #graph-minimap (a
                    one-line Static under the graph view) renders a
                    proportional bar plus a "ranks X-Y/Z" readout from
                    #graph-view's own real scroll_x/scroll_y/
                    container_size every tick — never a static
                    placeholder. #chat-input (M9 phase 6) is a free-form
                    Input below both views, always present regardless of
                    the `g` toggle. Every poll tick re-derives its
                    disabled/placeholder from ForgeApp.
                    active_chat_queue_for(self.run_id) — enabled with an
                    inviting placeholder while chattable, disabled with
                    an explanatory one otherwise. on_input_submitted
                    re-checks the queue again at submit time (the
                    message-arrives-right-as-the-attempt-finishes race):
                    present, the message is queued and the input clears;
                    gone, the attempt is rejected with a visible warning
                    notify and the typed text is left in place, never
                    cleared as if it had been sent. #live-trace-log
                    (post-M9 phase 3, a LIVE SESSION LabeledPanel of its
                    own, stacked full-width below the TASKS/TRACE row) is
                    a second RichLog, deliberately separate from
                    #trace-log — see "Live trace window, concretely"
                    below for why. _refresh_live_trace, called from the
                    same refresh_run poll tick right alongside
                    _refresh_chat_input, drains ForgeApp.
                    live_messages_for(self.run_id) through
                    render_live_message (forge/adapters/execution/
                    sdk_execution_adapter.py) and appends the resulting
                    lines, tracking its own self._live_queue reference
                    (not just the current live_messages_for value) across
                    polls so a message still queued the instant the
                    attempt ends still gets drained.
                    ModelEffortSwapModal + the `m` binding (post-M9 phase
                    4) — see "In-place model/effort swap, concretely"
                    below for the full mechanism this UI drives.
                    LiveRunScreen tracks _row_task_ids/
                    _live_execution_task_id/_live_node_type_name every
                    refresh_run poll tick (same shape as DashboardScreen.
                    _row_run_ids); check_action hides `m` from the footer
                    entirely whenever ForgeApp.active_chat_queue_for is
                    None, the same gate the chat input already uses, and
                    the action itself re-checks that plus requires the
                    TASKS table's current selection to actually be the
                    live task before opening the modal.
    dag_layout.py   compute_dag_layout(tasks, edges) -> DagLayout — pure,
                    zero Textual imports. rank = longest path from a root
                    (no incoming "dependency" edge), which spreads a
                    fan-out/fan-in shape (e.g. implement_exec ->
                    {unit_verify_exec, integration_test_write_exec ->
                    integration_verify_exec} -> deploy_exec) across ranks
                    correctly; lane is a task's simple, deterministic
                    position within its rank (tasks' own list order, no
                    crossing-minimization). Computed once per
                    GraphVersion.id and cached by LiveRunScreen, not
                    recomputed every poll tick. Detail-agnostic by design
                    — zoom lives entirely in dag_canvas.py.
    dag_canvas.py   render_dag_canvas(tasks, edges, layout, statuses,
                    style_for_status, detail="full", durations=None,
                    highlight_styles=None) -> rich.text.Text — a 2D
                    character grid (box-drawing/
                    Unicode lines, not SVG/pixels). "Zoom" on a character
                    grid means level of detail, not pixel scaling:
                    detail="full" draws a 5-row box (border/id/`● status`/
                    duration/border); detail="compact" draws a 3-row box
                    (border/`● id`/border) at roughly half the width — a
                    _DetailConfig per level holds box/gap sizing, and only
                    this module's own constants/content change between
                    them (dag_layout.py's rank/lane math never does).
                    Edges route through the gap columns between ranks (a
                    stub, a vertical run, a stub for a lane change), each
                    grid write landing only on a still-blank cell so a
                    multi-rank edge flying over an intervening rank's box
                    stops drawing across its footprint instead of
                    corrupting it. canvas_size(layout, detail) and
                    visible_rank_range(layout, detail, scroll_x,
                    viewport_width) are the pure geometry helpers
                    LiveRunScreen's zoom-reset and minimap depend on.
                    Colors come from the caller's style_for_status (built
                    from screens._status_style/App.get_css_variables()) —
                    this module never picks its own colors.
                    highlight_styles optionally maps task id -> a style
                    string that *replaces* the status-derived one for
                    that render — added for dag_builder_screen.py's
                    "selected"/"connect source"/"connect target" cursor
                    states, none of which are a run status; the caller
                    still owns the actual color, same as style_for_status.
    dag_builder_screen.py
                    DagBuilderScreen — the visual, keyboard-driven DAG
                    builder (M9 phase 5): place existing node types (from
                    NodeTypeRegistry.all_types()) onto a canvas, connect
                    them (`c`, navigate with `]`/`[`, `enter` to confirm —
                    appends the source task's id to the target's
                    depends_on), remove them (`x`, also stripping the
                    removed id from every other task's depends_on), edit
                    each one's description/acceptance_criterion_ids/
                    permission_profile override in a per-task inspector
                    (`i`), declare template metadata/inputs/acceptance
                    criteria (`m`/`f`/`v`), and save (`s`) to
                    `.forge/dags/<name>.json` in exactly the schema
                    custom_dag.py's loader expects. Renders with the same
                    dag_canvas.render_dag_canvas/dag_layout.
                    compute_dag_layout the live-run view uses — no second
                    renderer. Connect's cycle rejection reuses
                    compute_dag_layout directly (the candidate edge is
                    appended to a prospective edge list and handed to the
                    same function; a DagLayoutError means "would create a
                    cycle," surfaced via a visible, severity="error"
                    notify, never a silent no-op) rather than a second
                    cycle check. permission_profile overrides are
                    validated at save time with custom_dag.py's own
                    resolve_permission_profile. Creating a *new* template
                    only — loading an existing one back in for further
                    editing is out of scope for this pass. No
                    set_interval/poll loop at all: unlike LiveRunScreen,
                    there's no live run and nothing else mutates the
                    authored graph concurrently, so the canvas is just
                    re-rendered synchronously right after whatever
                    keypress or modal changed it.
```

## Resumability

Mechanical dispatch is synchronous: an attempt is written `RUNNING` before
it executes, then updated to `PASSED`/`FAILED` with its evidence in one
transaction. Single-process execution model, so any attempt still
`RUNNING` when a run resumes was abandoned by a prior process —
`reconcile_orphaned_attempts` cancels it so the scheduler dispatches fresh
rather than waiting on one that will never report back.

## Decisions and invalidation, concretely

`DecisionProposal` is bounded/typed, never trusted directly. `apply_decision`
is the only place one becomes a state change: `STOP_SUCCESS` re-derives
`all_required_gates_met()` itself; a retry must cite evidence, stay within
`max_retries_per_task`, and not just repeat the prior failure verbatim; an
escalation's model is checked against `ModelRegistry`. Any decision-adapter
failure falls back to a hand-written policy rather than hanging the run.

A `PROPOSE_GRAPH_REVISION` that invalidates evidence propagates: every
downstream task whose dependency chain consumed it gets *its* evidence
invalidated too (not just the one named) and its latest attempt superseded
— which doesn't block `_ready_tasks` the way PASSED does, so the scheduler
re-verifies instead of a completion check trusting stale work. The
*automatic* version (a later commit's diff touching the same paths an
older evidence row covered) is real-git-tested in `invalidation.py` but
not wired into live dispatch yet (see Known gaps).

## Model and effort selection, concretely

`select_model`/`select_effort` (`forge/controller/model_registry.py`) share
one failure-count policy, computed by a shared `_prior_failures` helper so
the two can't drift: 0–1 prior `FAILED` attempts for a task stays on
`default_model`/`default_effort`; 2+ escalates to
`escalation_model`/`escalation_effort`, falling back to the default if no
escalation value is configured. They diverge only on what "nothing
configured" means: every node type must have a `default_model` (no default
is `NoDefaultModelConfiguredError`), but `default_effort` is allowed to be
entirely unset — a node type that never configured any effort is the
common case, not an error, and `select_effort` returns `None` for it.

`dispatch_execution_attempt` calls `select_effort(task.id, node_type,
attempts)` on every EXECUTION dispatch (fresh attempts and decision-driven
retries/escalations alike, since it's recomputed from that dispatch's own
attempt history) and passes the result to `execution_runner(..., effort=
effort)`. `ExecutionRunner` is declared as a `Protocol` with a
keyword-only `effort: EffortLevel | None = None` parameter on its
`__call__` — not the plain `Callable[[...], ...]` alias it used to be,
since that shape can't express a keyword-only parameter at all (the same
`Protocol`-for-an-injected-callable pattern `DispatchRetryFn` in
`decisions.py` already uses). `run_sdk_node` and `RunWorker.
_run_sdk_node_with_chat` both forward `effort` straight through, same
shape as `chat_queue`; `sdk_execution_adapter.build_claude_agent_options`
is the one place it's translated into the real SDK's own
`ClaudeAgentOptions.effort` field (see the `adapters/execution/` entry
above for why `effort`, not `thinking`). `select_model` itself is not yet
called anywhere live — model selection for a fresh attempt still falls
back to `node_type.default_model` directly, and a retry/escalation's model
comes from the decision proposal, not `select_model` — this predates the
effort work and is unchanged by it.

**Task-level model/effort overrides (phase 2 of 4).** `TaskSpec`
(`forge/models/graph.py`) carries two optional fields a custom DAG's JSON
can set per task — `model_override: str | None` and `effort_override:
EffortLevel | None` — both `None` by default, identical to pre-phase-2
behavior. `forge/modes/custom_dag.py`'s `DagTaskSpec` exposes the JSON-facing
names (`model`, `effort`); a `model` override is validated at load time
against the real `ModelRegistry.validate_eligible` by the new
`resolve_model_override` (same shape, same `CustomDagError` convention, as
`resolve_permission_profile`'s existing `permission_profile` validation);
`effort` needs no equivalent check, since it's already constrained to a
real `EffortLevel` by its own `Literal` type the moment the file is
parsed. `build_custom_dag_request_and_graph` takes a `model_registry:
ModelRegistry` parameter for this purpose only — model/effort *selection*
stays entirely dispatch-time, this function only validates.

In `dispatch_execution_attempt` (`scheduler.py`):
- **Model**: `model = model_used or task.model_override or
  node_type.default_model` — the task-level override slots directly into
  the existing override-or-default expression, below a decision-driven
  retry/escalation's explicit `model_used` (a later, more specific
  instruction) and above the node type's own default. There is no
  escalation interaction to decide here, since a fresh dispatch's model
  never goes through `select_model`'s escalation policy at all regardless
  of any override (the pre-existing gap described two paragraphs up) —
  `model_override` works around that gap the same way everything else
  already does, it doesn't fix it.
- **Effort**: a task-level `effort_override` replaces only
  `node_type.default_effort`'s rung, never `escalation_effort`.
  `_effort_aware_node_type(task, node_type)` returns `node_type` unchanged
  with no override, or a transient `node_type.model_copy(update=
  {"default_effort": task.effort_override})` with one (`node_type` itself
  is never mutated); `select_effort` then runs its real failure-count
  policy against *that*, so `node_type.escalation_effort` still applies
  after 2+ prior `FAILED` attempts. This was a deliberate choice between
  two options — replace only the first rung (chosen) vs. an override that
  fixes effort permanently and disables escalation — made because a task
  author overriding the *starting* effort shouldn't also disable the
  safety net that exists precisely to recover from a starting point that
  turns out to be too weak; an override is a better first guess, not a
  promise that no failure will ever need more.

## In-place model/effort swap, concretely

Post-M9 phase 4, the last of the model/effort customization arc: lets a
person change a *currently-running* EXECUTION attempt's model and/or
effort without waiting for a retry. Two genuinely different mechanisms,
confirmed from the real SDK source, not assumed:

- **Model**: `ClaudeSDKClient.set_model(model)` is real, public,
  documented ("Change the AI model during conversation") — no interrupt,
  no reconnect, applies going forward on the same live session. The fast
  path: `RunWorker.model_swap_queue` -> `_watch_model_swap`
  (`sdk_execution_adapter.py`, same non-blocking-poll shape as
  `_forward_chat_messages`) -> `client.set_model()`.
- **Effort**: no SDK control request exists for it at all (checked every
  method in `claude_agent_sdk._internal.query.Query`'s control-request
  section). A real change needs `client.interrupt()`, the current turn
  ending, `disconnect()`, then a *new* `ClaudeSDKClient` opened with
  `resume=<session_id>` and the new `effort=` (an optional new `model=`
  rides along on the same reconnect rather than costing a second one).
  `client.interrupt()` itself only sends the interrupt control request and
  waits for the CLI's acknowledgement of *that request* (confirmed from
  `ClaudeSDKClient.interrupt()`/`Query.interrupt()`'s source) — the turn's
  own end still arrives later as an ordinary terminal `ResultMessage` on
  the same stream the receive loop already consumes, whose
  `terminal_reason` the SDK documents as `"aborted_streaming"`/
  `"aborted_tools"` for exactly this case. This is what lets
  `_watch_effort_swap` just wait on the receive loop's own existing
  `stop_event` rather than special-casing an interrupted turn — modeled,
  not just asserted, in the fake `ClaudeSDKClient` double this feature's
  tests use. `_INTERRUPT_RESULT_TIMEOUT_SECONDS` (30s) bounds that wait;
  on timeout it also directly cancels the receive task itself (merely
  giving up on the wait would not have unblocked `run_execution_attempt`,
  which is separately blocked on `await receive_task`).

`EffortSwapRequest` (`forge/controller/execution_registry.py`, a frozen
dataclass: `effort: EffortLevel`, `model: str | None = None`) is the
validated request that rides `RunWorker.effort_swap_queue` and comes back
out on `ExecutionRunOutcome.swap_requested`. When a fired request carries
no `model` of its own, `_watch_effort_swap` defaults it to whatever
`_watch_model_swap` most recently applied (a shared one-element
`current_model` list, seeded with the attempt's own dispatched model) —
so a model swap that fired earlier in the same attempt survives into the
effort swap's own reconnect instead of being silently lost, and "just
change effort, leave the model alone" does exactly that even when no
model swap happened at all.

An attempt that gets interrupted this way is recorded as a new terminal
`AttemptStatus.INTERRUPTED_FOR_SWAP` — neither PASSED (no evidence) nor
FAILED (the session didn't error) — with no evidence row and
`failure_summary=None`. `sdk_session_id` (the field every EXECUTION
attempt already carries) is reused as the session to resume from;
`pending_resume_model`/`pending_resume_effort` (new `Attempt` fields)
carry the person's request forward. These live on `Attempt`, not
`TaskSpec.model_override`/`effort_override` (the phase-2 custom-DAG
override fields, structurally the same shape) — deliberately: those are
graph-authored data on an immutable, append-only `GraphVersion`, and a
live, one-dispatch-only override has no business mutating that. An
`Attempt` is this project's own append-only history row for exactly one
attempt, the right scope for "what should this task's next dispatch do
differently."

`_ready_tasks` (`scheduler.py`) already excludes a task from dispatch only
when its latest attempt is RUNNING/PASSED/FAILED — INTERRUPTED_FOR_SWAP
isn't in that set, so the task becomes ready again automatically, the same
way CANCELLED/SUPERSEDED already do, with no change needed there.
`dispatch_execution_attempt` looks up the task's immediately prior attempt
before computing anything; when it's INTERRUPTED_FOR_SWAP, this one
dispatch uses its `sdk_session_id` as `resume_session_id` and its
`pending_resume_model`/`pending_resume_effort` in place of the normal
override-or-default model expression and `select_effort`'s escalation
policy — bypassed entirely for this dispatch only, since the user
explicitly chose these, not a failure-driven escalation.
`_prior_failures`/`_has_unrecoverable_failure`/
`_failed_tasks_needing_decision` all already filter on `==
AttemptStatus.FAILED` specifically, so INTERRUPTED_FOR_SWAP was never at
risk of being miscounted — confirmed by a dedicated test, not just
inferred from reading the filters.

`forge.controller.decisions.request_execution_swap` is the one validated
path a swap request goes through — mirrors `apply_decision`'s own rule
that nothing outside the controller layer turns intent into effect. It
checks a given `model` against `ModelRegistry.validate_eligible` (same
check `apply_decision` runs for a decision's own `escalate_to_model`) and
routes by what's being asked for: a model-only request goes to
`model_swap_queue`; any request carrying an `effort` goes to
`effort_swap_queue` as one `EffortSwapRequest`. `ForgeApp.
request_execution_swap` is the only caller — `LiveRunScreen`'s `m` binding
opens `ModelEffortSwapModal` (two `OptionList`s — model, filtered to
`ModelRegistry.all_capabilities()` eligible for this node type; effort,
from `EffortLevel`'s own `Literal` args — each with a real "(no change)"
option, plus explicit Confirm/Cancel, since this modal collects two
choices rather than dismissing on the first selection) and hands its
result to that one call; the screen never pokes a `RunWorker`'s queues
directly.

A request still sitting on `model_swap_queue`/`effort_swap_queue` when an
attempt ends normally (no swap ever happened) is simply never read again
once `RunWorker` clears the attribute back to `None` in its `finally` —
same staleness answer `active_chat_queue` already gave, confirmed again
here with dedicated tests for both new queues.

## EXECUTION dispatch, concretely

`dispatch_execution_attempt` creates a real worktree, builds a prompt from
the node type's own `build_input`/`build_prompt`, and calls whatever
`ExecutionRunner` is configured — but never trusts that runner's own
report of what it did. Afterward it commits any dirty changes left in the
worktree as a safety net, reads the actual resulting commit, and diffs it
against the base commit for `changed_paths` — `result_commit` and
`changed_paths` are facts derived from git, not claims a node makes about
itself. A `MECHANICAL` task that depends on an EXECUTION one (e.g.
`unit_verify_exec` on `implement_exec`) runs inside that same worktree
rather than getting a fresh one — it's verifying that output, not
starting over.

A Claude Agent SDK attempt's real session id (the UUID the SDK's terminal
`ResultMessage` carries) is captured the same way: `sdk_execution_adapter.
run_execution_attempt` pulls it off the drained `messages` list into
`ExecutionResult.session_id`, `sdk_node_runner.run_sdk_node` threads it into
`ExecutionRunOutcome.session_id`, and `dispatch_execution_attempt` sets
`Attempt.sdk_session_id` from that outcome, same as `result_commit`. This is
capture-and-expose only — `forge inspect <run_id> <task_id>` prints it
(`claude --resume <sdk_session_id>`, for a person to run themselves);
nothing in Forge reads it back or passes it into a later attempt's
`ClaudeAgentOptions`. Always `None` for a MECHANICAL attempt or a Codex CLI
one.

## Live trace window, concretely

A third cross-thread `queue.Queue`, alongside `active_chat_queue`'s
message-in and the attempt-result's data-out: `RunWorker.live_messages:
queue.Queue[Any] | None` (`forge/tui/runner.py`), same lifecycle as
`active_chat_queue` — created fresh and assigned just before
`_run_sdk_node_with_chat` calls `run_sdk_node(..., live_messages=...)`,
cleared back to `None` in the same `finally`, so being driven through this
wrapper is still the one "this attempt is chattable" signal, now for two
channels instead of one. `run_execution_attempt`'s receive loop
(`sdk_execution_adapter.py`) does a non-blocking `live_messages.put_nowait(
message)` right after appending each message to its own `messages` list —
opposite direction from `chat_queue`, same non-blocking spirit. Unbounded
on purpose: one turn's `receive_response()` yields a small, human-paced
number of discrete message objects, not raw token deltas, so no drop
policy is needed. `LiveRunScreen._refresh_live_trace` keeps its own
`self._live_queue` reference across polls (independent of `ForgeApp.
live_messages_for`'s current value) specifically so a message still
sitting on the queue the instant `RunWorker` clears `live_messages` back
to `None` still gets one final drain instead of being stranded.

`render_live_message` (kept in `sdk_execution_adapter.py`, not
`forge/tui/*`, so only `forge/adapters/execution/*` ever imports real
`claude_agent_sdk` types) turns one message into zero or more short lines
— assistant text, a tool call (name plus a one-line key-argument summary,
reusing the same `_PATH_INPUT_KEY_BY_TOOL` map the real PreToolUse hook
already uses), a tool result (pass/fail plus a short summary, never a raw
dump), a one-word `ThinkingBlock` marker, and a one-line terminal
`ResultMessage` marker; a `SystemMessage` and other internal-plumbing
kinds render nothing. `LiveRunScreen` shows this in its own `LIVE SESSION`
panel (`#live-trace-log`), deliberately separate from `#trace-log`:
`#trace-log` is `compute_trace_entries`'s sorted, durable, `Store`-backed
whole-run replay; the live panel is an unsorted, ephemeral,
arrival-order stream scoped to one chattable attempt's lifetime, with no
real timestamp to sort by. Both panels are driven from the same 0.75s
`refresh_run` poll tick, each with its own incremental-append tracking.
Claude-only (Codex adapter untouched), same reasoning as every other
model/effort/observability phase in this arc.

## Enforcement, concretely

- Claude Agent SDK attempts: a PreToolUse hook, generated per attempt from
  the task's `PermissionProfile` and worktree path, is the actual boundary
  — not the node's prompt. Built and real: `forge/isolation/hooks.py`
  (the policy decision) wired into the real SDK's types by
  `forge/adapters/execution/sdk_execution_adapter.py`. PreToolUse is the one
  point in the SDK's permission pipeline that runs unconditionally, before
  `permission_mode`/`allowed_tools` are even consulted — confirmed from the
  SDK's own source and docstrings, not assumed.
- Codex CLI attempts: no hook equivalent exists, so these always run
  inside a Docker container mounting only that attempt's worktree —
  `run_codex_attempt` raises `ContainerRequiredError` immediately if no
  container is supplied; there is no configuration path around this.
  `run_codex_in_sandbox` owns the full lifecycle and guarantees
  `stop_container` runs even when the attempt itself fails.
- `Bash(<glob>)` allowlist entries in a `PermissionProfile` match against
  the *entire* command string (`hooks.py::_tool_covered_by_allowlist`,
  `fnmatch` with no implicit wildcard), not just a leading token — a
  narrowly-scoped pattern like `Bash(npx*)` denies any compound command
  (`cd ... && npx ...`, `mkdir -p ... && npx ...`) regardless of what
  follows the `&&`. A live M7 run surfaced this concretely: discovered
  when `manual_test_default`'s original narrow allowlist denied nearly
  every tool call `manual_test_exec` attempted. `denied_bash_patterns`
  (`git push*`/`*vercel deploy*`/`curl *`/`rm -rf *`, every profile's
  default) is the actual intended backstop for a profile that needs real
  shell flexibility — a narrow `allowed_tools` glob is not a substitute
  for it and can silently block legitimate work instead.

## M7 descoped slice, concretely

`forge/modes/feature_blast.py` builds one deterministic chain —
`implement_exec → unit_verify_exec → deploy_exec → manual_test_exec` — with
no LLM-driven `plan_decision` step yet; the shape itself is fixed by that
function, not proposed. `deploy_exec` is `MECHANICAL`: it reads every text
file out of its upstream worktree (inherited through the same
worktree-reuse chain `unit_verify_exec` already uses) and calls
`VercelDeployAdapter.deploy()` for real. `manual_test_exec` then needs a
live URL to test against that it can't derive itself —
`_manual_test_build_input` (in `forge/nodes/__init__.py`) bridges this by
pulling `deployment_url` off the upstream `deploy_exec` output before
calling `manual_test_exec.build_input`.

`manual_test_exec`'s pass/fail is a judgment call, not something a git diff
can express — this is why `NodeType` gained `output_strategy`
(`"commit_diff"` default vs. `"result_file"`). A `"result_file"` node
writes `.forge_result.json` into its own worktree before finishing;
`dispatch_execution_attempt` reads and validates that file against the
node's `output_schema` instead of deriving output from the commit diff,
still never trusting anything said in the session's own transcript.

`scripts/live_feature_blast_m7.py` is the live verification entry point
(same pattern as `live_smoke_test.py`): scaffolds a throwaway Vercel-Python
toy app, runs the full chain against a real `claude` CLI session and a
real Vercel deployment, then independently curls the live URL itself
rather than trusting the run's own `COMPLETED_VERIFIED`.

A first live run surfaced a real gap: `deploy_exec` retried a rejected
Vercel token 3 times, identically, before giving up — the generic
`retry_or_escalate_policy` has no way to know a failure is unrecoverable
rather than just unlucky. Fixed via `Attempt.requires_user_input`/
`MechanicalResult.requires_user_input`: a mechanical runner that can tell
its own failure can't be fixed by retrying (deploy_exec, via
`vercel_adapter.py`'s new `VercelCredentialError` — raised for a 401/403
auth rejection, distinct from an ordinary `VercelAPIError`) sets this flag,
and `scheduler._resolve_failed_tasks` routes a FAILED attempt with it
straight to a synthesized `REQUEST_USER_INPUT` decision
(`_unrecoverable_failure_proposal`) — no decision adapter call, no retry
budget spent, one attempt then `BLOCKED_AWAITING_INPUT`. This generalizes
beyond deploy_exec: any mechanical node can opt into the same fast path by
setting the flag on its own `MechanicalResult`.

**Known gaps:**

- `scripts/live_smoke_test.py` has run successfully with real credentials
  (`implement_exec` → `unit_verify_exec`, `completed_verified`), so
  EXECUTION dispatch and the PreToolUse hook path work live end to end. A
  hook actually *denying* a disallowed tool call has not been exercised
  live yet (the smoke test's task never attempted one).
- The commit-based automatic invalidation rule (`invalidation.py`) has real
  commits to work with as of M6 but is not wired into the scheduler yet.
- The Docker/Codex CLI path is tested only by mocking `subprocess.run`, and
  its JSONL event-schema assumption has not been checked against a real
  `codex exec --json` run.

## Status

See `tasks.md` for the milestone-by-milestone build order (M0–M8) and what's
actually done vs. planned.
