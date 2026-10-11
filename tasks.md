# Tasks

Milestones from the plan doc, §9. Build order matters — don't skip ahead.

- [x] **M0 — Repo setup + core models + validator.**
  - [x] venv (plain `.venv`, no pyinit/direnv available on this machine —
        see `CLAUDE.md`), requirements.txt/requirements-dev.txt, black config
  - [x] CLAUDE.md / requirements.md / architecture.md / tasks.md
  - [x] Pydantic models (`forge/models/*.py`): common, request, graph,
        attempt, evidence, decision, permissions, model_registry
  - [x] `NodeTypeRegistry` / `PermissionProfileRegistry` (`forge/controller/node_registry.py`)
  - [x] `compile_and_validate` + acyclicity checks + `propagate_invalidation`
        (`forge/controller/validator.py`)
  - [x] 24 pytest cases (`tests/validator/`, `tests/models/`) covering
        structural/type/dependency/gate/invalidation/permission invariants
        plus the schema-level permission-escalation test (plan §10 test #3)
        — all pass, `black`-formatted
  - [ ] initial commit + push to a GitHub remote (pending: `gh` CLI isn't
        installed on this machine, and creating/pushing to a public remote
        needs a quick check-in per the plan's own note — see next session)
- [ ] **M1** — controller skeleton + SQLite persistence + mechanical-only nodes, resumability
- [ ] **M2** — worktree isolation + Claude Agent SDK adapter + PreToolUse hooks
- [ ] **M3** — decision adapter + bounded `DecisionAction` + `apply_decision`
- [ ] **M4** — graph revision + invalidation propagation
- [ ] **M5** — Codex CLI adapter + Docker-sandbox isolation
- [ ] **M6** — three-tier testing nodes (`integration_test_write_exec`, `manual_test_exec`)
- [ ] **M7** — Feature Blast end-to-end with a real Vercel deployment (flagship milestone)
- [ ] **M8** — CLI polish + `forge trace`/`forge inspect`

Everything past M8 (other modes, a third adapter, dashboard) is backlog —
see `requirements.md` — not scheduled.

## Open item for next session

Confirm with the user: create the GitHub remote now (public, per their
earlier answer) and push, or keep working with local commits a while
longer. `gh` CLI would need installing first if we go that route from here.
