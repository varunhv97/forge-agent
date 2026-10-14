# Forge — project instructions

Forge is a personal coding agent whose execution is governed by a strict,
code-enforced DAG (not a prompt asking an LLM to follow a workflow). Design
and scope live in `requirements.md` and `architecture.md`; progress in
`tasks.md`. Read all three at session start.

## Core invariant

Nodes (execution and decision) never write to the graph, their own
permissions, or run state — they produce typed proposals. Only
`forge/controller/*` validates and applies them. If you're tempted to let a
node "just also" update status/schedule/permissions directly, stop — that's
exactly the pattern this project exists to prevent.

## Environment

- Python 3.11+ (`claude-agent-sdk` requires >=3.10). Work inside a venv:
  `python3.11 -m venv .venv && source .venv/bin/activate`, then
  `pip install -r requirements-dev.txt`.
- Every module using `X | Y` union syntax still needs
  `from __future__ import annotations` at the top (keeps the codebase
  consistent).
- Format with `black .`, lint with `ruff check .`, type-check with `mypy`
  (strict for `forge/` and `scripts/`), test with `pytest`. CI runs all four;
  configuration lives in `pyproject.toml`.
- New code is fully annotated. Prefer a `Protocol` over a concrete class for
  injected dependencies, and justify any `# noqa` or `Any` inline.
- Machine-specific setup notes, if any, go in `CLAUDE.local.md` (gitignored).

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
