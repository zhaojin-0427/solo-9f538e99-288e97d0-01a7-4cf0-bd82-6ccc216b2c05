"""Business service: reporting, refinement and P95 queries."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import db
from .domain import (
    Bucket,
    TargetSpec,
    ValidationError,
    p95_rank,
    validate_disjoint,
    validate_subdivision,
)

Key = tuple[str, str, str]  # (line_id, production_day ISO, coil_id)


@dataclass(frozen=True)
class CoilState:
    line_id: str
    coil_id: str
    production_day: date
    version: int
    buckets: tuple[Bucket, ...]


def _key(line_id: str, production_day: date, coil_id: str) -> Key:
    return (line_id, production_day.isoformat(), coil_id)


def _load_buckets(conn: sqlite3.Connection, key: Key) -> tuple[Bucket, ...]:
    rows = conn.execute(
        "SELECT lo, hi, count FROM buckets "
        "WHERE line_id = ? AND production_day = ? AND coil_id = ? ORDER BY position",
        key,
    ).fetchall()
    return tuple(Bucket(lo=r["lo"], hi=r["hi"], count=r["count"]) for r in rows)


def _write_summary(
    conn: sqlite3.Connection,
    key: Key,
    buckets: list[Bucket],
    version: int,
) -> None:
    line_id, day, coil_id = key
    conn.execute(
        "INSERT INTO coils (line_id, production_day, coil_id, version) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (line_id, production_day, coil_id) DO UPDATE SET "
        "version = excluded.version, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (line_id, day, coil_id, version),
    )
    conn.execute(
        "DELETE FROM buckets "
        "WHERE line_id = ? AND production_day = ? AND coil_id = ?",
        key,
    )
    conn.executemany(
        "INSERT INTO buckets (line_id, production_day, coil_id, position, lo, hi, count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (line_id, day, coil_id, pos, b.lo, b.hi, b.count)
            for pos, b in enumerate(buckets)
        ],
    )


def _current_version(conn: sqlite3.Connection, key: Key) -> int:
    row = conn.execute(
        "SELECT version FROM coils "
        "WHERE line_id = ? AND production_day = ? AND coil_id = ?",
        key,
    ).fetchone()
    if row is None:
        raise ValidationError("unknown coil report")
    return int(row["version"])


def submit_reports(
    conn: sqlite3.Connection,
    reports: list[tuple[str, date, str, list[Bucket]]],
) -> list[CoilState]:
    """Create or replace day summaries for several coils in one transaction.

    Each element is ``(line_id, production_day, coil_id, buckets)``.  Reporting
    the same key twice again replaces it.  The whole batch is all-or-nothing:
    if any report fails validation, nothing is written and prior data stays.
    """
    if not reports:
        raise ValidationError("at least one coil report is required")
    prepared: list[tuple[Key, date, list[Bucket]]] = []
    seen: set[Key] = set()
    for line_id, production_day, coil_id, buckets in reports:
        if not line_id or not line_id.strip():
            raise ValidationError("line_id must not be empty")
        if not coil_id or not coil_id.strip():
            raise ValidationError("coil_id must not be empty")
        if not buckets:
            raise ValidationError(f"coil {coil_id} needs at least one bucket")
        key = _key(line_id, production_day, coil_id)
        if key in seen:
            raise ValidationError(
                f"coil {coil_id} reported twice for {key[1]} on line {line_id}"
            )
        seen.add(key)
        validate_disjoint(buckets)
        prepared.append((key, production_day, list(buckets)))

    states: list[CoilState] = []
    with db.transaction(conn):
        for key, production_day, buckets in prepared:
            row = conn.execute(
                "SELECT version FROM coils "
                "WHERE line_id = ? AND production_day = ? AND coil_id = ?",
                key,
            ).fetchone()
            version = 1 if row is None else int(row["version"]) + 1
            _write_summary(conn, key, buckets, version)
            states.append(
                CoilState(key[0], key[2], production_day, version, tuple(buckets))
            )
    return states


def refine_bucket(
    conn: sqlite3.Connection,
    *,
    line_id: str,
    production_day: date,
    coil_id: str,
    target: TargetSpec,
    children: list[Bucket],
) -> CoilState:
    """Replace one existing bucket by narrower sub-buckets, atomically.

    The target interval ``[lo, hi]`` must match a current bucket of that day
    summary; when the target also carries a count it must match too, so a stale
    refinement cannot silently rewrite the wrong data.  On any rule violation
    the transaction rolls back and the previously reported buckets remain.
    """
    key = _key(line_id, production_day, coil_id)
    with db.transaction(conn):
        version = _current_version(conn, key) + 1
        current = _load_buckets(conn, key)
        matches = [b for b in current if b.lo == target.lo and b.hi == target.hi]
        if not matches:
            raise ValidationError(
                f"bucket [{target.lo},{target.hi}] is not part of the current report"
            )
        original = matches[0]
        if not target.matches(original):
            raise ValidationError(
                f"bucket count is {original.count}, not the reported {target.count}"
            )
        validate_subdivision(original, children)

        refined = [b for b in current if b != original] + list(children)
        validate_disjoint(refined)
        ordered = sorted(refined, key=lambda b: (b.lo, b.hi))
        _write_summary(conn, key, ordered, version)
    return CoilState(line_id, coil_id, production_day, version, tuple(ordered))


def get_coil(
    conn: sqlite3.Connection, *, line_id: str, production_day: date, coil_id: str
) -> CoilState:
    key = _key(line_id, production_day, coil_id)
    with db.snapshot(conn):
        version = _current_version(conn, key)
        buckets = _load_buckets(conn, key)
    return CoilState(line_id, coil_id, production_day, version, buckets)


@dataclass(frozen=True)
class Candidate:
    coil_id: str
    production_day: date
    bucket: Bucket


@dataclass(frozen=True)
class LineP95:
    line_id: str
    start: date
    end: date
    total: int | None
    rank: int | None
    minimum: int | None
    maximum: int | None
    # ``None`` when there is no data or when both bounds coincide; the spec
    # only asks for candidate buckets when the two ends differ.
    candidates: tuple[Candidate, ...] | None

    @property
    def has_data(self) -> bool:
        return self.total is not None


SCOPED = """
    SELECT b.coil_id, b.production_day, b.position, b.lo, b.hi, b.count
    FROM buckets b
    JOIN coils c
      ON c.line_id = b.line_id
     AND c.production_day = b.production_day
     AND c.coil_id = b.coil_id
    WHERE c.line_id = ? AND c.production_day BETWEEN ? AND ?
"""


def query_p95(
    conn: sqlite3.Connection,
    *,
    line_id: str,
    start: date,
    end: date,
    min_intersection: int = 2,
) -> LineP95:
    """P95 range and candidate buckets for a line over an inclusive day range.

    Only day summaries (bucket counts) are read; no measurement is expanded.
    The aggregation happens in SQL so a long range never materialises one
    Python object per bucket.  The lower bound, upper bound, rank and bucket
    list all come from a single snapshot transaction, hence from the same
    committed submission.

    With ``k = ceil(0.95 * n)``:

    * smallest P95 = the ``k``-th smallest bucket lower bound, weights = counts;
    * largest P95  = the ``k``-th smallest bucket upper bound, weights = counts.

    A bucket is a candidate when its overlap with ``[minimum, maximum]`` spans
    at least ``min_intersection`` integer thicknesses.
    """
    if start > end:
        raise ValidationError("start date must not be after end date")
    if min_intersection < 1:
        raise ValidationError("min_intersection must be at least 1")

    params = (line_id, start.isoformat(), end.isoformat())
    with db.snapshot(conn):
        totals = conn.execute(
            f"WITH scoped AS ({SCOPED}) "
            "SELECT COALESCE(SUM(count), 0) AS n FROM scoped",
            params,
        ).fetchone()
        total = int(totals["n"])
        if total == 0:
            return LineP95(line_id, start, end, None, None, None, None, None)
        rank = p95_rank(total)

        minimum = conn.execute(
            f"WITH scoped AS ({SCOPED}), "
            "running AS (SELECT lo, SUM(count) OVER (ORDER BY lo) AS cum FROM scoped) "
            "SELECT lo FROM running WHERE cum >= ? ORDER BY lo LIMIT 1",
            (*params, rank),
        ).fetchone()["lo"]
        maximum = conn.execute(
            f"WITH scoped AS ({SCOPED}), "
            "running AS (SELECT hi, SUM(count) OVER (ORDER BY hi) AS cum FROM scoped) "
            "SELECT hi FROM running WHERE cum >= ? ORDER BY hi LIMIT 1",
            (*params, rank),
        ).fetchone()["hi"]

        candidates: tuple[Candidate, ...] | None = None
        if minimum != maximum:
            rows = conn.execute(
                f"WITH scoped AS ({SCOPED}) "
                "SELECT coil_id, production_day, lo, hi, count FROM scoped "
                "WHERE MIN(hi, ?) - MAX(lo, ?) + 1 >= ? "
                "ORDER BY production_day, coil_id, position",
                (*params, maximum, minimum, min_intersection),
            ).fetchall()
            candidates = tuple(
                Candidate(
                    row["coil_id"],
                    date.fromisoformat(row["production_day"]),
                    Bucket(lo=row["lo"], hi=row["hi"], count=row["count"]),
                )
                for row in rows
            )

    return LineP95(
        line_id,
        start,
        end,
        total,
        rank,
        minimum,
        maximum,
        candidates,
    )
