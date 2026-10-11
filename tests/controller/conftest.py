from __future__ import annotations

from pathlib import Path

import pytest

from forge.controller import db
from forge.controller.store import Store


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "forge.db"


@pytest.fixture
def store(db_path: Path) -> Store:
    conn = db.connect(db_path)
    try:
        yield Store(conn)
    finally:
        conn.close()
