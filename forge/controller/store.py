from __future__ import annotations

import sqlite3

from forge.controller.db import transaction
from forge.models.attempt import Attempt
from forge.models.evidence import Evidence
from forge.models.graph import GraphVersion
from forge.models.request import Request
from forge.models.run import Run
from forge.models.trace import TraceEvent


class NotFoundError(LookupError):
    """Raised when a store lookup finds no matching row."""


class Store:
    """
    Thin persistence layer over SQLite. Every write method owns its own
    transaction boundary; callers never issue raw SQL or manage commits
    themselves. Rows carry the full model as a JSON blob (the Pydantic
    model is the source of truth for shape) plus a handful of indexed
    columns used in WHERE clauses, so query performance doesn't depend on
    keeping a second, hand-maintained column schema in sync with the model.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        self._conn.close()

    # -- requests -------------------------------------------------------

    def save_request(self, request: Request) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO requests (id, data) VALUES (?, ?)",
                (request.id, request.model_dump_json()),
            )

    def load_request(self, request_id: str) -> Request:
        row = self._conn.execute("SELECT data FROM requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"request '{request_id}' not found")
        return Request.model_validate_json(row["data"])

    # -- graph versions ---------------------------------------------------

    def save_graph_version(self, graph: GraphVersion) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO graph_versions (id, request_id, version, status, data) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    graph.id,
                    graph.request_id,
                    graph.version,
                    graph.status,
                    graph.model_dump_json(),
                ),
            )

    def load_graph_version(self, graph_version_id: str) -> GraphVersion:
        row = self._conn.execute(
            "SELECT data FROM graph_versions WHERE id = ?", (graph_version_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"graph version '{graph_version_id}' not found")
        return GraphVersion.model_validate_json(row["data"])

    def load_latest_graph_version(self, request_id: str) -> GraphVersion:
        row = self._conn.execute(
            "SELECT data FROM graph_versions WHERE request_id = ? ORDER BY version DESC LIMIT 1",
            (request_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no graph version found for request '{request_id}'")
        return GraphVersion.model_validate_json(row["data"])

    def load_graph_versions(self, request_id: str) -> list[GraphVersion]:
        rows = self._conn.execute(
            "SELECT data FROM graph_versions WHERE request_id = ? ORDER BY version ASC",
            (request_id,),
        ).fetchall()
        return [GraphVersion.model_validate_json(r["data"]) for r in rows]

    # -- runs -------------------------------------------------------------

    def save_run(self, run: Run) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO runs (id, request_id, status, data) VALUES (?, ?, ?, ?)",
                (run.id, run.request_id, run.status, run.model_dump_json()),
            )

    def update_run(self, run: Run) -> None:
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "UPDATE runs SET status = ?, data = ? WHERE id = ?",
                (run.status, run.model_dump_json(), run.id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"run '{run.id}' not found")

    def load_run(self, run_id: str) -> Run:
        row = self._conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"run '{run_id}' not found")
        return Run.model_validate_json(row["data"])

    # -- attempts ---------------------------------------------------------

    def save_attempt(self, attempt: Attempt) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO attempts (id, run_id, task_id, status, attempt_number, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    attempt.id,
                    attempt.run_id,
                    attempt.task_id,
                    attempt.status,
                    attempt.attempt_number,
                    attempt.model_dump_json(),
                ),
            )

    def update_attempt(self, attempt: Attempt) -> None:
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "UPDATE attempts SET status = ?, data = ? WHERE id = ?",
                (attempt.status, attempt.model_dump_json(), attempt.id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"attempt '{attempt.id}' not found")

    def record_attempt_result(self, attempt: Attempt, evidence: list[Evidence]) -> None:
        """
        Updates an attempt's final status and inserts the evidence it
        produced in a single transaction — a crash can never leave a
        PASSED/FAILED attempt without its evidence, or vice versa.
        """
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "UPDATE attempts SET status = ?, data = ? WHERE id = ?",
                (attempt.status, attempt.model_dump_json(), attempt.id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"attempt '{attempt.id}' not found")
            for e in evidence:
                conn.execute(
                    "INSERT INTO evidence (id, run_id, task_id, data) VALUES (?, ?, ?, ?)",
                    (e.id, e.run_id, e.task_id, e.model_dump_json()),
                )

    def load_attempts(self, run_id: str) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT data FROM attempts WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [Attempt.model_validate_json(r["data"]) for r in rows]

    # -- evidence -----------------------------------------------------------

    def save_evidence(self, evidence: Evidence) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO evidence (id, run_id, task_id, data) VALUES (?, ?, ?, ?)",
                (evidence.id, evidence.run_id, evidence.task_id, evidence.model_dump_json()),
            )

    def load_evidence(self, run_id: str) -> list[Evidence]:
        rows = self._conn.execute(
            "SELECT data FROM evidence WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [Evidence.model_validate_json(r["data"]) for r in rows]

    def update_evidence(self, evidence: Evidence) -> None:
        """
        Overwrites an existing evidence row — used to set
        `invalidated_by_evidence_id` once something (a graph revision's
        explicit invalidation, or later, the commit-based staleness check)
        determines it should no longer count toward a completion gate.
        """
        with transaction(self._conn) as conn:
            cursor = conn.execute(
                "UPDATE evidence SET data = ? WHERE id = ?",
                (evidence.model_dump_json(), evidence.id),
            )
            if cursor.rowcount == 0:
                raise NotFoundError(f"evidence '{evidence.id}' not found")

    # -- trace events ---------------------------------------------------------

    def append_trace_event(self, event: TraceEvent) -> None:
        with transaction(self._conn) as conn:
            conn.execute(
                "INSERT INTO trace_events (id, run_id, created_at, data) VALUES (?, ?, ?, ?)",
                (event.id, event.run_id, event.created_at.isoformat(), event.model_dump_json()),
            )

    def load_trace_events(self, run_id: str) -> list[TraceEvent]:
        rows = self._conn.execute(
            "SELECT data FROM trace_events WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        return [TraceEvent.model_validate_json(r["data"]) for r in rows]
