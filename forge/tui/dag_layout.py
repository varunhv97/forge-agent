"""
Pure topological layout for the live-run DAG view. Takes a `GraphVersion`'s
`tasks`/`edges` (see `forge/models/graph.py`) and assigns each task a
`(rank, lane)` position:

- `rank` is the longest path from a root (a task with no incoming
  `"dependency"` edge) — this is what gives the left-to-right ordering and
  naturally spreads a fan-out/fan-in shape like `implement_exec ->
  {unit_verify_exec, integration_test_write_exec -> integration_verify_exec}
  -> deploy_exec -> manual_test_exec` across ranks (both branches of the
  fan-out land at the same rank; the fan-in task's rank is forced past
  whichever branch is longer).
- `lane` is just a position within a rank, assigned deterministically by
  the order tasks appear in `tasks` — this deliberately does not try to
  minimize edge crossings or otherwise lay things out prettily.

Zero Textual (or any UI) imports on purpose: this only needs the task/edge
shape, so it's unit-testable on its own and only needs to be recomputed
once per `GraphVersion` (the shape doesn't change mid-run, only per-task
status does — that's layered on top by `forge/tui/dag_canvas.py`).
"""

from __future__ import annotations

from dataclasses import dataclass

from forge.models.graph import EdgeSpec, TaskSpec


class DagLayoutError(ValueError):
    """Raised if `tasks`/`edges` describe a cycle — compile_and_validate
    should already have rejected this, but this module stands on its own
    and refuses to loop forever rather than assume that's always true."""


@dataclass(frozen=True)
class NodePosition:
    task_id: str
    rank: int
    lane: int


@dataclass(frozen=True)
class DagLayout:
    """
    `positions` is keyed by task id. `lane_counts[r]` is how many lanes
    rank `r` actually uses — a renderer needs this to know the grid's
    overall shape without re-scanning `positions` itself.
    """

    positions: dict[str, NodePosition]
    num_ranks: int
    lane_counts: dict[int, int]


def compute_dag_layout(tasks: list[TaskSpec], edges: list[EdgeSpec]) -> DagLayout:
    task_ids = [t.id for t in tasks]
    id_set = set(task_ids)

    parents: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for e in edges:
        if e.relation != "dependency":
            continue
        # Edges referencing an unknown task are a validator concern
        # (_check_edges_reference_existing_tasks); this module just
        # ignores anything it can't place rather than raising on it.
        if e.from_task not in id_set or e.to_task not in id_set:
            continue
        parents[e.to_task].append(e.from_task)

    rank: dict[str, int] = {}

    def _rank_of(task_id: str, visiting: set[str]) -> int:
        if task_id in rank:
            return rank[task_id]
        if task_id in visiting:
            raise DagLayoutError(f"cycle detected at task '{task_id}'")
        visiting.add(task_id)
        ps = parents.get(task_id, [])
        computed = 0 if not ps else 1 + max(_rank_of(p, visiting) for p in ps)
        visiting.discard(task_id)
        rank[task_id] = computed
        return computed

    for tid in task_ids:
        _rank_of(tid, set())

    # Lane: a simple stable count of how many tasks at this rank have
    # already been placed, walked in `tasks`' own order.
    lane_counts: dict[int, int] = {}
    positions: dict[str, NodePosition] = {}
    for tid in task_ids:
        r = rank[tid]
        lane = lane_counts.get(r, 0)
        positions[tid] = NodePosition(task_id=tid, rank=r, lane=lane)
        lane_counts[r] = lane + 1

    num_ranks = (max(rank.values()) + 1) if rank else 0
    # Every rank in range should have an entry, even a rank with zero
    # tasks placed in it (shouldn't happen given how rank is derived, but
    # a renderer indexing lane_counts[r] for every r in range(num_ranks)
    # shouldn't have to guard against a missing key either).
    full_lane_counts = {r: lane_counts.get(r, 0) for r in range(num_ranks)}

    return DagLayout(positions=positions, num_ranks=num_ranks, lane_counts=full_lane_counts)
