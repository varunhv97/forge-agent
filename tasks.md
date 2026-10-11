# Tasks

Milestones from the plan doc's roadmap. Build order matters — don't skip ahead.

- [x] **M0 — Repo setup + core models + validator.**
  - [x] venv, requirements.txt/requirements-dev.txt, black config (see
        `CLAUDE.md`'s Environment section — this got revisited in M2)
  - [x] CLAUDE.md / requirements.md / architecture.md / tasks.md
  - [x] Pydantic models (`forge/models/*.py`): common, request, graph,
        attempt, evidence, decision, permissions, model_registry
  - [x] `NodeTypeRegistry` / `PermissionProfileRegistry` (`forge/controller/node_registry.py`)
  - [x] `compile_and_validate` + acyclicity checks + `propagate_invalidation`
        (`forge/controller/validator.py`)
  - [x] 24 pytest cases (`tests/validator/`, `tests/models/`) covering
        structural/type/dependency/gate/invalidation/permission invariants
        plus the schema-level permission-escalation test
        — all pass, `black`-formatted
  - [x] initial commit + pushed to `https://github.com/varunhv97/forge-agent`
        (branch `main`)
- [x] **M1 — Controller skeleton + SQLite persistence + mechanical-only nodes, resumability.**
  - [x] `Run`/`TraceEvent` models (`forge/models/run.py`, `forge/models/trace.py`);
        `run_id` added to `Attempt`/`Evidence` for querying
  - [x] SQLite schema + explicit transactions (`forge/controller/db.py`)
  - [x] `Store`: typed CRUD over requests/graph_versions/runs/attempts/evidence/
        trace_events, with atomic attempt+evidence writes (`forge/controller/store.py`)
  - [x] `MechanicalRunnerRegistry` (`forge/controller/mechanical.py`)
  - [x] Scheduler: readiness, dispatch, `all_required_gates_met`, orphaned-attempt
        reconciliation on restart (`forge/controller/scheduler.py`)
  - [x] 15 pytest cases (`tests/controller/`), including a simulated crash
        mid-dispatch (kill the process after an attempt is marked RUNNING,
        reopen a fresh connection, resume) proving no duplicate dispatch and
        no lost work. Found and fixed a real bug in the process: a vacuously-
        true gate check could mask an unrecoverable task failure.
  - [x] commit + push
- [x] **M2 — Worktree isolation + Claude Agent SDK adapter + PreToolUse hooks.**
  - [x] Python environment migrated 3.9.6 → 3.11.16: `claude-agent-sdk`
        requires >=3.10, which system Python didn't have and this machine
        has no brew/pyenv to get it — a standalone CPython build downloaded
        into `.runtimes/` is the workaround; see `CLAUDE.md`. Full suite
        re-verified green on the new interpreter before building further.
  - [x] `forge/isolation/worktree_manager.py` — `create_worktree`/
        `remove_worktree` over real git worktrees, idempotent cleanup,
        10 tests against a real git repo (built via parallel subagent)
  - [x] `forge/isolation/hooks.py` — the PreToolUse policy decision engine
        (`ToolCallEvent` → `HookDecision`), adapter-agnostic, tool
        allowlist + explicit deny patterns + network heuristic + realpath-
        based path-scope check (catches `../` and symlink escapes),
        13 tests (built via parallel subagent)
  - [x] `forge/adapters/execution/sdk_execution_adapter.py` — wires the
        above into real `claude_agent_sdk` types: translates
        `PreToolUseHookInput` → `ToolCallEvent` and `HookDecision` back to
        the SDK's `permissionDecision` shape, builds `ClaudeAgentOptions`
        (cwd/model/allowed_tools/hooks), and `run_execution_attempt` calls
        `claude_agent_sdk.query` for real. 13 tests.
  - [ ] **Known gap, not closed:** the actual live acceptance scenario
        ("a node tries `git push`, the hook blocks it") needs a real model
        call, which needs network + `ANTHROPIC_API_KEY` or an authenticated
        `claude` CLI session — neither is available in this sandbox. What's
        tested instead: every translation/construction function offline,
        plus pulling the *exact* callback `ClaudeAgentOptions` would hand
        to the SDK and invoking it the way the SDK's documented contract
        says it will be, confirming it denies a disallowed command. The
        untested part is specifically "does the real `claude` CLI subprocess
        actually call this callback as advertised" — verified against the
        SDK's source/type definitions, not against a live run. Revisit once
        an API key is available to run a true end-to-end check.
  - [ ] commit (adapter piece) + push
- [ ] **M3** — decision adapter + bounded `DecisionAction` + `apply_decision`
- [ ] **M4** — graph revision + invalidation propagation
- [ ] **M5** — Codex CLI adapter + Docker-sandbox isolation
- [ ] **M6** — three-tier testing nodes (`integration_test_write_exec`, `manual_test_exec`)
- [ ] **M7** — Feature Blast end-to-end with a real Vercel deployment (flagship milestone)
- [ ] **M8** — CLI polish + `forge trace`/`forge inspect`

Everything past M8 (other modes, a third adapter, dashboard) is backlog —
see `requirements.md` — not scheduled.

## Notes

- This machine's home directory is locked down for writes/reads from this
  tool's shell (even with the sandbox override) — `gh` lives at
  `.gh-bin` with `GH_CONFIG_DIR=.gh-config` (both gitignored) instead of
  the usual `~/.local/bin` / `~/.config/gh`. Pushes still need to be run by
  the user directly; this tool's shell cannot reach git credentials.
