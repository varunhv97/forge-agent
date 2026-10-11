from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS graph_versions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    UNIQUE (request_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_run_id ON attempts (run_id);
CREATE INDEX IF NOT EXISTS idx_attempts_task_id ON attempts (run_id, task_id);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_run_id ON evidence (run_id);
CREATE INDEX IF NOT EXISTS idx_evidence_task_id ON evidence (run_id, task_id);

CREATE TABLE IF NOT EXISTS trace_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_events_run_id ON trace_events (run_id, created_at);
"""


def connect(path: Union[str, Path]) -> sqlite3.Connection:
    """
    Opens (and initializes, if needed) the Forge database at `path`. WAL
    mode keeps a crash mid-write from corrupting the file and keeps reads
    from blocking on an in-flight write; transactions are managed
    explicitly via `transaction()` rather than sqlite3's implicit ones.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """
    One explicit SQLite transaction per call site. Writes that must land
    together — an attempt's final status alongside the evidence it
    produced — go inside the same `with transaction(conn):` block so a
    crash can never leave one without the other.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
