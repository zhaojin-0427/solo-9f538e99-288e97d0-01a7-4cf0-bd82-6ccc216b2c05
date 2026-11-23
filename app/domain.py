"""Domain rules for coating-thickness buckets.

A bucket is a closed integer-micrometre interval ``[lo, hi]`` together with a
positive measurement count.  For a day summary we never materialise individual
measurements: the only information we keep is, per bucket, how many readings
fell into it.  Everything in this module therefore works on bucket counts.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence


class ValidationError(ValueError):
    """Raised when a request violates the domain rules."""


# SQLite stores integers as signed 64-bit and aggregates counts with SUM, so
# values must stay far below 2**63.  These caps are many orders of magnitude
# above any real coil (a metre is 10**6 micrometres; a coil has ~10**6 points)
# while keeping rank arithmetic and aggregation overflow-free.
MAX_THICKNESS = 10**9
MAX_COUNT = 10**12


@dataclass(frozen=True)
class Bucket:
    lo: int
    hi: int
    count: int

    def __post_init__(self) -> None:
        # ``bool`` is an ``int`` subclass, but a bucket bound or count of
        # ``True``/``False`` is never a legitimate thickness or count.
        if type(self.lo) is not int or type(self.hi) is not int:
            raise ValidationError("bucket bounds must be integers")
        if type(self.count) is not int:
            raise ValidationError("bucket count must be an integer")
        if self.lo > self.hi:
            raise ValidationError("bucket lower bound must not exceed upper bound")
        if abs(self.lo) > MAX_THICKNESS or abs(self.hi) > MAX_THICKNESS:
            raise ValidationError(
                f"bucket bounds must be within +/-{MAX_THICKNESS} micrometres"
            )
        if self.count <= 0:
            raise ValidationError("bucket count must be positive")
        if self.count > MAX_COUNT:
            raise ValidationError(f"bucket count must not exceed {MAX_COUNT}")

    @property
    def size(self) -> int:
        return self.hi - self.lo + 1

    def contains(self, other: "Bucket") -> bool:
        return self.lo <= other.lo and other.hi <= self.hi


def validate_disjoint(buckets: Sequence[Bucket]) -> None:
    """Buckets of one submission must not overlap in thickness."""
    ordered = sorted(buckets, key=lambda b: (b.lo, b.hi))
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.lo <= prev.hi:
            raise ValidationError(
                f"buckets [{prev.lo},{prev.hi}] and [{nxt.lo},{nxt.hi}] overlap"
            )


def validate_subdivision(original: Bucket, children: Sequence[Bucket]) -> None:
    """Children must sit inside their parent, be disjoint and keep the total."""
    if not children:
        raise ValidationError("subdivision must contain at least one bucket")
    validate_disjoint(children)
    for child in children:
        if not original.contains(child):
            raise ValidationError(
                f"bucket [{child.lo},{child.hi}] is not inside [{original.lo},{original.hi}]"
            )
    total = sum(child.count for child in children)
    if total != original.count:
        raise ValidationError(
            f"subdivision counts sum to {total}, expected {original.count}"
        )


@dataclass(frozen=True)
class TargetSpec:
    """Identifies the existing bucket a refinement replaces.

    ``count`` is optional: when supplied it must equal the stored count, which
    protects against refining a bucket the caller did not actually see.
    """

    lo: int
    hi: int
    count: int | None = None

    def __post_init__(self) -> None:
        if type(self.lo) is not int or type(self.hi) is not int:
            raise ValidationError("target bounds must be integers")
        if self.lo > self.hi:
            raise ValidationError("target lower bound must not exceed upper bound")
        if abs(self.lo) > MAX_THICKNESS or abs(self.hi) > MAX_THICKNESS:
            raise ValidationError(
                f"target bounds must be within +/-{MAX_THICKNESS} micrometres"
            )
        if self.count is not None:
            if type(self.count) is not int:
                raise ValidationError("target count must be an integer")
            if self.count <= 0 or self.count > MAX_COUNT:
                raise ValidationError(
                    f"target count must be between 1 and {MAX_COUNT}"
                )

    def matches(self, bucket: Bucket) -> bool:
        return (
            bucket.lo == self.lo
            and bucket.hi == self.hi
            and (self.count is None or bucket.count == self.count)
        )


def p95_rank(total: int) -> int:
    """1-based rank of the P95 order statistic: ceil(0.95 * n)."""
    if total <= 0:
        raise ValidationError("no measurements")
    return (95 * total + 99) // 100


def kth_value(pairs: Iterable[tuple[int, int]], rank: int) -> int:
    """Value of the ``rank``-th smallest reading.

    ``pairs`` are ``(value, count)`` pairs, one per bucket.  Using ``lo`` for
    every bucket gives the smallest possible P95, using ``hi`` the largest:
    within a bucket we are free to place its readings anywhere in the interval.
    """
    remaining = rank
    for value, count in sorted(pairs):
        remaining -= count
        if remaining <= 0:
            return value
    raise ValidationError("rank exceeds the number of measurements")


def overlap_values(bucket: Bucket, minimum: int, maximum: int) -> int:
    """Number of integer thicknesses shared by a bucket and ``[min, max]``."""
    low = max(bucket.lo, minimum)
    high = min(bucket.hi, maximum)
    return max(0, high - low + 1)


@dataclass(frozen=True)
class P95Range:
    total: int
    rank: int
    minimum: int
    maximum: int

    @property
    def degenerate(self) -> bool:
        return self.minimum == self.maximum


def p95_range(buckets: Sequence[Bucket]) -> P95Range:
    """Minimum and maximum possible P95 over the given bucket counts."""
    total = sum(bucket.count for bucket in buckets)
    if total == 0:
        raise ValidationError("no measurements")
    rank = p95_rank(total)
    minimum = kth_value(((b.lo, b.count) for b in buckets), rank)
    maximum = kth_value(((b.hi, b.count) for b in buckets), rank)
    return P95Range(total=total, rank=rank, minimum=minimum, maximum=maximum)


# --------------------------------------------------------------------------- #
# Instrument day table <-> coil report reconciliation
# --------------------------------------------------------------------------- #
#
# The instrument packs a whole production day into buckets; each coil report
# packs its own points into buckets.  The two sides need not use the same
# bucket boundaries.  A point from an instrument bucket can be paired with a
# point from a coil bucket exactly when the two closed integer intervals share
# at least one micrometre value; every point may be paired only once.  A day
# reconciles only when *all* points on both sides can be paired.
#
# This is the maximum-cardinality matching between two multisets of intervals
# with capacities.  It is computed by one sweep over bucket boundaries.  An
# interval that ends first is the most constrained, so it is matched first
# against still-open intervals of the other side.  Only per-bucket counters are
# kept: time and extra memory are O(number of buckets), never O(points).


@dataclass(frozen=True)
class PairingResult:
    """Outcome of pairing an instrument day table against coil reports."""

    instrument_total: int
    coil_total: int
    paired: int

    @property
    def instrument_unpaired(self) -> int:
        return self.instrument_total - self.paired

    @property
    def coil_unpaired(self) -> int:
        return self.coil_total - self.paired

    @property
    def complete(self) -> bool:
        return self.instrument_unpaired == 0 and self.coil_unpaired == 0


def pair_buckets(
    instrument: Sequence[Bucket], coils: Sequence[Bucket]
) -> PairingResult:
    """Maximum number of points pairable across the two bucket multisets.

    Two buckets overlap when ``max(lo) <= min(hi)``, i.e. they share an integer
    micrometre value.  ``paired`` is the size of a maximum bipartite matching
    (a point of the instrument side matched to a distinct point of the coil
    side).  The remaining counts on each side are the *minimum* numbers of
    points that cannot be paired.

    The sweep visits bucket starts and ends in ascending thickness order.  At a
    coordinate where instrument buckets end, each such bucket greedily pairs
    with the still-open coil buckets (which necessarily contain that
    coordinate), and vice versa.  Pairing expiring buckets first is optimal:
    they have the fewest future opportunities.  Only two heaps of bucket
    entries plus boundary dictionaries are kept, so memory is proportional to
    the number of buckets on both sides.
    """

    # Per coordinate and side, the bucket entries that start / end there.
    # Each entry is ``[hi, remaining]`` and is shared between the two maps;
    # ``remaining`` is decremented as points are paired.
    starts: dict[int, tuple[list[list[int]], list[list[int]]]] = {}
    ends: dict[int, tuple[list[list[int]], list[list[int]]]] = {}
    instrument_total = 0
    coil_total = 0

    for side, buckets in ((0, instrument), (1, coils)):
        for bucket in buckets:
            entry = [bucket.hi, bucket.count]
            starts.setdefault(bucket.lo, ([], []))[side].append(entry)
            ends.setdefault(bucket.hi, ([], []))[side].append(entry)
            if side == 0:
                instrument_total += bucket.count
            else:
                coil_total += bucket.count

    instrument_open: list[tuple[int, int, list]] = []
    coil_open: list[tuple[int, int, list]] = []
    tick = 0
    paired = 0

    def push(heap: list, entry: list) -> None:
        nonlocal tick
        # ``tick`` keeps heap ordering deterministic when ``hi`` ties.
        heapq.heappush(heap, (entry[0], tick, entry))
        tick += 1

    def consume(heap: list, needed: int) -> int:
        """Pair up to ``needed`` points from still-open opposite buckets."""

        nonlocal paired
        taken = 0
        while needed > 0 and heap:
            entry = heap[0][2]
            if entry[1] <= 0:  # lazily discard exhausted entries
                heapq.heappop(heap)
                continue
            amount = needed if needed < entry[1] else entry[1]
            entry[1] -= amount
            needed -= amount
            taken += amount
            if entry[1] <= 0:
                heapq.heappop(heap)
        paired += taken
        return taken

    coordinates = set(starts)
    coordinates.update(ends)
    for coordinate in sorted(coordinates):
        beginning = starts.get(coordinate)
        if beginning is not None:
            for entry in beginning[0]:
                push(instrument_open, entry)
            for entry in beginning[1]:
                push(coil_open, entry)

        ending = ends.get(coordinate)
        if ending is not None:
            # Instrument buckets ending here pair with coil buckets that are
            # still open (they started at or before this coordinate and have
            # not ended yet, hence they contain it).
            for entry in ending[0]:
                if entry[1] > 0:
                    consume(coil_open, entry[1])
                    entry[1] = 0
                    # ``entry`` was not necessarily at the heap front; stale
                    # entries are skipped lazily by ``consume``.
            for entry in ending[1]:
                if entry[1] > 0:
                    consume(instrument_open, entry[1])
                    entry[1] = 0

    return PairingResult(instrument_total, coil_total, paired)


@dataclass(frozen=True)
class DayMismatch:
    """One (line, production day) whose instrument and coil points do not pair.

    ``instrument_unpaired`` / ``coil_unpaired`` are the minimum numbers of
    points that cannot be paired, given a maximum matching.  ``*_total`` are the
    bucket-count totals on each side.  A day reconciles exactly when both
    unpaired counts are zero.
    """

    line_id: str
    production_day: date
    instrument_total: int
    coil_total: int
    instrument_unpaired: int
    coil_unpaired: int


class ReconciliationError(ValidationError):
    """Raised when submitted data cannot be fully paired per day table.

    The whole request is rejected.  ``mismatches`` lists every affected
    (line, production day) with the minimum unpaired counts on both sides.
    """

    def __init__(self, mismatches: Sequence[DayMismatch]):
        self.mismatches = tuple(mismatches)
        parts = [
            f"line {m.line_id} on {m.production_day.isoformat()} does not reconcile: "
            f"at least {m.instrument_unpaired} of {m.instrument_total} instrument "
            f"point(s) and {m.coil_unpaired} of {m.coil_total} coil point(s) "
            f"cannot be paired"
            for m in self.mismatches
        ]
        super().__init__("; ".join(parts))
