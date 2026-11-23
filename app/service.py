"""Business service: reporting, refinement and P95 queries."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from . import db
from .domain import (
    Bucket,
    DayMismatch,
    ReconciliationError,
    TargetSpec,
    ValidationError,
    pair_buckets,
    p95_rank,
    validate_disjoint,
    validate_subdivision,
)

Key = tuple[str, str, str]  # (line_id, production_day ISO, coil_id)
DayKey = tuple[str, str]  # (line_id, production_day ISO)


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


# --------------------------------------------------------------------------- #
# Instrument day tables
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DayTableState:
    line_id: str
    production_day: date
    version: int
    buckets: tuple[Bucket, ...]


def _load_day_table(
    conn: sqlite3.Connection, day_key: DayKey
) -> DayTableState | None:
    row = conn.execute(
        "SELECT version FROM instrument_days "
        "WHERE line_id = ? AND production_day = ?",
        day_key,
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        "SELECT lo, hi, count FROM instrument_buckets "
        "WHERE line_id = ? AND production_day = ? ORDER BY position",
        day_key,
    ).fetchall()
    return DayTableState(
        line_id=day_key[0],
        production_day=date.fromisoformat(day_key[1]),
        version=int(row["version"]),
        buckets=tuple(Bucket(r["lo"], r["hi"], r["count"]) for r in rows),
    )


def _write_day_table(
    conn: sqlite3.Connection,
    day_key: DayKey,
    buckets: list[Bucket],
    version: int,
) -> None:
    conn.execute(
        "INSERT INTO instrument_days (line_id, production_day, version) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT (line_id, production_day) DO UPDATE SET "
        "version = excluded.version, "
        "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (*day_key, version),
    )
    conn.execute(
        "DELETE FROM instrument_buckets "
        "WHERE line_id = ? AND production_day = ?",
        day_key,
    )
    conn.executemany(
        "INSERT INTO instrument_buckets "
        "(line_id, production_day, position, lo, hi, count) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (day_key[0], day_key[1], pos, b.lo, b.hi, b.count)
            for pos, b in enumerate(buckets)
        ],
    )


def _coil_buckets_for_day(
    conn: sqlite3.Connection, day_key: DayKey
) -> list[Bucket]:
    rows = conn.execute(
        "SELECT b.lo, b.hi, b.count FROM buckets b "
        "JOIN coils c ON c.line_id = b.line_id "
        "AND c.production_day = b.production_day AND c.coil_id = b.coil_id "
        "WHERE b.line_id = ? AND b.production_day = ? "
        "ORDER BY b.coil_id, b.position",
        day_key,
    ).fetchall()
    return [Bucket(r["lo"], r["hi"], r["count"]) for r in rows]


def _effective_day_mismatches(
    conn: sqlite3.Connection, day_keys: set[DayKey]
) -> list[DayMismatch]:
    """Return one mismatch per day that has a day table but does not pair.

    Always reads the *committed-or-current-transaction* rows, so callers that
    have just written their changes (coil summaries, a day table, a refinement)
    validate the whole effective day.  Days without an instrument day table are
    unconstrained.  A day table with no (or too few) coil points is a mismatch:
    every point of the instrument side is then unpaired.
    """
    mismatches: list[DayMismatch] = []
    for day_key in sorted(day_keys):
        table = _load_day_table(conn, day_key)
        if table is None:
            continue
        coils = _coil_buckets_for_day(conn, day_key)
        result = pair_buckets(list(table.buckets), coils)
        if result.complete:
            continue
        mismatches.append(
            DayMismatch(
                line_id=day_key[0],
                production_day=date.fromisoformat(day_key[1]),
                instrument_total=result.instrument_total,
                coil_total=result.coil_total,
                instrument_unpaired=result.instrument_unpaired,
                coil_unpaired=result.coil_unpaired,
            )
        )
    return mismatches


def _assert_effective_days_reconcile(
    conn: sqlite3.Connection, day_keys: set[DayKey]
) -> None:
    """Raise if any affected day with a day table fails to pair completely."""
    mismatches = _effective_day_mismatches(conn, day_keys)
    if mismatches:
        raise ReconciliationError(mismatches)


def _day_key(line_id: str, production_day: date) -> DayKey:
    return (line_id, production_day.isoformat())


def _prepare_day_table(
    line_id: str, production_day: date, buckets: list[Bucket]
) -> tuple[DayKey, date, list[Bucket]]:
    """Validate one day-table payload and return its normalised form."""
    if not line_id or not line_id.strip():
        raise ValidationError("line_id must not be empty")
    if not buckets:
        raise ValidationError("an instrument day table needs at least one bucket")
    validate_disjoint(buckets)
    ordered = sorted(buckets, key=lambda b: (b.lo, b.hi))
    return _day_key(line_id, production_day), production_day, ordered


def _write_day_table_versioned(
    conn: sqlite3.Connection, day_key: DayKey, buckets: list[Bucket]
) -> int:
    row = conn.execute(
        "SELECT version FROM instrument_days "
        "WHERE line_id = ? AND production_day = ?",
        day_key,
    ).fetchone()
    version = 1 if row is None else int(row["version"]) + 1
    _write_day_table(conn, day_key, buckets, version)
    return version


def submit_day_table(
    conn: sqlite3.Connection,
    *,
    line_id: str,
    production_day: date,
    buckets: list[Bucket],
) -> DayTableState:
    """Register or replace the instrument day table for one (line, day).

    The table is accepted only when, after the write, *all* instrument points
    and *all* coil-report points of that day can be paired.  A day table whose
    points are not yet covered by coil reports is therefore rejected; submit
    both together to bootstrap a day.  The write and any rejection are atomic,
    so on failure the previous day table, coil reports and P95 stay readable.
    """
    day_key, production_day, ordered = _prepare_day_table(
        line_id, production_day, buckets
    )
    with db.transaction(conn):
        version = _write_day_table_versioned(conn, day_key, ordered)
        _assert_effective_days_reconcile(conn, {day_key})
    return DayTableState(line_id, production_day, version, tuple(ordered))


def get_day_table(
    conn: sqlite3.Connection, *, line_id: str, production_day: date
) -> DayTableState:
    day_key = _day_key(line_id, production_day)
    with db.snapshot(conn):
        state = _load_day_table(conn, day_key)
    if state is None:
        raise ValidationError("unknown instrument day table")
    return state


def submit_day_tables_and_reports(
    conn: sqlite3.Connection,
    *,
    day_tables: list[tuple[str, date, list[Bucket]]],
    reports: list[tuple[str, date, str, list[Bucket]]],
) -> tuple[list[DayTableState], list[CoilState]]:
    """Register day tables and coil reports together, atomically.

    Everything is written inside one transaction and then every affected day
    that ends up with a day table is reconciled against the *whole effective*
    day.  This lets a day table and its coil reports be submitted at the same
    time and still be accepted only when the effective data pairs completely;
    any mismatch rolls the whole request back.
    """
    if not day_tables and not reports:
        raise ValidationError("at least one day table or coil report is required")

    prepared_tables: list[tuple[DayKey, date, list[Bucket], str]] = []
    seen_days: set[DayKey] = set()
    for line_id, production_day, buckets in day_tables:
        day_key, production_day, ordered = _prepare_day_table(
            line_id, production_day, buckets
        )
        if day_key in seen_days:
            raise ValidationError(
                f"day table for {day_key[1]} on line {day_key[0]} submitted twice"
            )
        seen_days.add(day_key)
        prepared_tables.append((day_key, production_day, ordered, line_id))

    prepared_reports = _prepare_reports(reports, allow_empty=True)

    table_states: list[DayTableState] = []
    coil_states: list[CoilState] = []
    with db.transaction(conn):
        for day_key, production_day, ordered, line_id in prepared_tables:
            version = _write_day_table_versioned(conn, day_key, ordered)
            table_states.append(
                DayTableState(line_id, production_day, version, tuple(ordered))
            )
        coil_states = _write_reports(conn, prepared_reports)
        affected = {day_key for day_key, _, _, _ in prepared_tables}
        affected.update((key[0], key[1]) for key, _, _ in prepared_reports)
        _assert_effective_days_reconcile(conn, affected)
    return table_states, coil_states


def _prepare_reports(
    reports: list[tuple[str, date, str, list[Bucket]]],
    *,
    allow_empty: bool = False,
) -> list[tuple[Key, date, list[Bucket]]]:
    """Validate a batch of coil day summaries and reject duplicate keys."""
    if not reports and not allow_empty:
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
    return prepared


def _write_reports(
    conn: sqlite3.Connection, prepared: list[tuple[Key, date, list[Bucket]]]
) -> list[CoilState]:
    """Write already-validated coil summaries; caller owns the transaction."""
    states: list[CoilState] = []
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


def submit_reports(
    conn: sqlite3.Connection,
    reports: list[tuple[str, date, str, list[Bucket]]],
) -> list[CoilState]:
    """Create or replace day summaries for several coils in one transaction.

    Each element is ``(line_id, production_day, coil_id, buckets)``.  Reporting
    the same key twice again replaces it.  The whole batch is all-or-nothing:
    if any report fails validation, nothing is written and prior data stays.
    For every affected day that already has an instrument day table, the whole
    effective day (all coils after this submission) must still pair completely;
    otherwise the batch is rejected and the previous data stays.
    """
    prepared = _prepare_reports(reports)
    with db.transaction(conn):
        states = _write_reports(conn, prepared)
        # If any affected day already has an instrument day table, the whole
        # *effective* day (all coils after this submission) must still pair.
        _assert_effective_days_reconcile(
            conn, {(key[0], key[1]) for key, _, _ in prepared}
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
        # Refining changes how the coil's buckets are cut, so a day with an
        # instrument table must still pair completely after the change.
        _assert_effective_days_reconcile(
            conn, {_day_key(line_id, production_day)}
        )
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
