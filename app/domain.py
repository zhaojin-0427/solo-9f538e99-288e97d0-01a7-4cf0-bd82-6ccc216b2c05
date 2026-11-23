"""Domain rules for coating-thickness buckets.

A bucket is a closed integer-micrometre interval ``[lo, hi]`` together with a
positive measurement count.  For a day summary we never materialise individual
measurements: the only information we keep is, per bucket, how many readings
fell into it.  Everything in this module therefore works on bucket counts.
"""

from __future__ import annotations

from dataclasses import dataclass
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
