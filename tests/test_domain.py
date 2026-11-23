import pytest

from app.domain import (
    Bucket,
    TargetSpec,
    ValidationError,
    p95_range,
    p95_rank,
    validate_disjoint,
    validate_subdivision,
)


def test_rank_is_ceiling_of_95_percent():
    assert p95_rank(1) == 1
    assert p95_rank(20) == 19
    assert p95_rank(21) == 20
    assert p95_rank(100) == 95
    assert p95_rank(101) == 96


def test_bucket_rejects_bad_bounds_and_counts():
    for lo, hi, count in [(5, 4, 1), (1, 2, 0), (1, 2, -3)]:
        with pytest.raises(ValidationError):
            Bucket(lo=lo, hi=hi, count=count)


def test_overlapping_flat_buckets_rejected():
    with pytest.raises(ValidationError):
        validate_disjoint([Bucket(1, 5, 2), Bucket(5, 9, 2)])


def test_adjacent_flat_buckets_allowed():
    validate_disjoint([Bucket(1, 5, 2), Bucket(6, 9, 2)])


def test_subdivision_rules():
    parent = Bucket(10, 20, 30)
    validate_subdivision(parent, [Bucket(10, 14, 12), Bucket(15, 20, 18)])
    with pytest.raises(ValidationError):  # child outside parent
        validate_subdivision(parent, [Bucket(9, 14, 12), Bucket(15, 20, 18)])
    with pytest.raises(ValidationError):  # overlapping children
        validate_subdivision(parent, [Bucket(10, 15, 12), Bucket(15, 20, 18)])
    with pytest.raises(ValidationError):  # wrong total
        validate_subdivision(parent, [Bucket(10, 14, 12), Bucket(15, 20, 17)])


def test_p95_range_uses_weighted_bucket_edges():
    # 10 readings in [1,2], 10 in [10,11]; n=20, rank=19.
    # Smallest possible: all lows -> 1..10=10 values, then [10,11] lows (10)
    # -> the 19th is 10.  Largest: highs -> [1,2]=10, then [10,11] highs (10)
    # -> the 19th is 11.
    result = p95_range([Bucket(1, 2, 10), Bucket(10, 11, 10)])
    assert (result.total, result.rank) == (20, 19)
    assert (result.minimum, result.maximum) == (10, 11)
    assert not result.degenerate


def test_p95_range_degenerate_when_one_bucket():
    result = p95_range([Bucket(5, 5, 100)])
    assert result.minimum == result.maximum == 5
    assert result.degenerate


def test_p95_range_rank_lands_inside_bucket():
    # lows: 100 in [1,100] -> rank 95 is 1.  highs -> 100.
    result = p95_range([Bucket(1, 100, 100)])
    assert (result.minimum, result.maximum) == (1, 100)


def test_target_spec_count_is_optional():
    bucket = Bucket(1, 10, 20)
    assert TargetSpec(1, 10).matches(bucket)
    assert TargetSpec(1, 10, 20).matches(bucket)
    assert not TargetSpec(1, 10, 19).matches(bucket)
    assert not TargetSpec(2, 10).matches(bucket)
    with pytest.raises(ValidationError):
        TargetSpec(10, 1)
    with pytest.raises(ValidationError):
        TargetSpec(1, 10, 0)


def test_bucket_rejects_bool_values():
    with pytest.raises(ValidationError):
        Bucket(lo=True, hi=5, count=1)
    with pytest.raises(ValidationError):
        Bucket(lo=1, hi=True, count=1)
    with pytest.raises(ValidationError):
        Bucket(lo=1, hi=5, count=True)


def test_counts_and_bounds_are_bounded():
    from app.domain import MAX_COUNT, MAX_THICKNESS

    Bucket(0, MAX_THICKNESS, MAX_COUNT)  # at the limits is fine
    with pytest.raises(ValidationError):
        Bucket(0, MAX_THICKNESS + 1, 1)
    with pytest.raises(ValidationError):
        Bucket(0, 5, MAX_COUNT + 1)
    with pytest.raises(ValidationError):
        TargetSpec(0, 5, MAX_COUNT + 1)
