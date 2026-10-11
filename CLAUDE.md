# Forge — project instructions

Forge is a personal coding agent whose execution is governed by a strict,
code-enforced DAG (not a prompt asking an LLM to follow a workflow). Full
design: `/Users/varvenka/.claude/plans/pasted-content-id-aee6-plan-a-wondrous-stream.md`.
Read `requirements.md`, `architecture.md`, `tasks.md` at session start.

## Core invariant

Nodes (execution and decision) never write to the graph, their own
permissions, or run state — they produce typed proposals. Only
`forge/controller/*` validates and applies them. If you're tempted to let a
node "just also" update status/schedule/permissions directly, stop — that's
exactly the pattern this project exists to prevent.

## Environment

- This machine has no `pyinit`, `direnv`, `brew`, or `pyenv` — only system
  `/usr/bin/python3` (3.9.6). Venv lives at `.venv/` (gitignored); activate
  with `source .venv/bin/activate` before running anything.
- Because we're on 3.9, every module using `X | Y` union syntax needs
  `from __future__ import annotations` at the top. Don't drop it.
- Install deps with `pip install -r requirements-dev.txt` inside the venv
  (not bare `pip3 install` against system Python).
- Format with `black .`; run tests with `pytest`.

## Working agreement

- Build iteratively: v1 = core engine + Feature Blast mode only. Every other
  mode is a backlog entry — do not build one without discussing
  it with the user first.
- Node types live in one global registry (`forge/controller/node_registry.py`)
  shared across modes — never give a mode its own private node type when an
  existing one's input/output contract already fits.
- Follow the milestone order in `tasks.md`. Each milestone's acceptance
  criterion must be independently checked (a passing test, an actual curl
  against a deployed URL, etc.) before marking it done — not just "the code
  is written."
