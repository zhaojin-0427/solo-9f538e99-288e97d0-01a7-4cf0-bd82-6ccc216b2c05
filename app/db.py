"""SQLite persistence.

A day summary is keyed by ``(line_id, production_day, coil_id)``: the same coil
number may legitimately appear on several production days, and each day's
buckets are one summary.  Its buckets live in a child table.

A refinement rewrites one summary's buckets in a single ``BEGIN IMMEDIATE``
transaction, so either all new sub-buckets and the new version land together,
or nothing changes.  Reads use a single deferred transaction, so the P95 bounds
and the bucket list come from one snapshot.

No individual measurement is ever stored or expanded: only bucket counts.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS coils (
    line_id        TEXT NOT NULL,
    production_day TEXT NOT NULL,
    coil_id        TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (line_id, production_day, coil_id)
);

CREATE TABLE IF NOT EXISTS buckets (
    line_id        TEXT NOT NULL,
    production_day TEXT NOT NULL,
    coil_id        TEXT NOT NULL,
    position       INTEGER NOT NULL,
    lo             INTEGER NOT NULL,
    hi             INTEGER NOT NULL,
    count          INTEGER NOT NULL CHECK (count > 0),
    PRIMARY KEY (line_id, production_day, coil_id, position),
    CHECK (lo <= hi),
    FOREIGN KEY (line_id, production_day, coil_id)
        REFERENCES coils (line_id, production_day, coil_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_coils_day
    ON coils (line_id, production_day);
"""

DEFAULT_DATABASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "coating.db"
)


def database_path() -> str:
    return os.environ.get("DATABASE_PATH", DEFAULT_DATABASE_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or database_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        # ``executescript`` commits implicitly, so it must not run inside an
        # explicit BEGIN block.  Every statement is IF NOT EXISTS and thus
        # safe to replay on every start.
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Exclusive write transaction: commit on success, roll back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Read transaction: every SELECT inside sees the same committed state."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")
