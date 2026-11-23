"""Instrument day tables and reconciliation with coil reports.

The instrument packs a whole production day into buckets; each coil report
packs its own points into buckets.  A point on either side may be paired with
one point on the other side when their closed integer-micrometre intervals
share at least one value.  A day reconciles only when *every* point on both
sides is paired.
"""

import random

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.domain import Bucket, PairingResult, pair_buckets
from app.main import create_app


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "instrument.db"
    init_db(str(path))
    return str(path)


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client


def coil(client, line="L1", day="2026-11-20", coil_id="C1", buckets=None):
    return client.post(
        "/reports",
        json={
            "line_id": line,
            "production_day": day,
            "coil_id": coil_id,
            "buckets": buckets or [{"lo": 1, "hi": 10, "count": 100}],
        },
    )


def bulk(client, reports):
    return client.post("/reports/bulk", json={"reports": reports})


def day_table(client, line="L1", day="2026-11-20", buckets=None):
    return client.post(
        "/day-tables",
        json={
            "line_id": line,
            "production_day": day,
            "buckets": buckets or [{"lo": 1, "hi": 10, "count": 100}],
        },
    )


def read_table(client, line="L1", day="2026-11-20"):
    return client.get("/day-tables/" + line, params={"production_day": day})


def stored(client_db_path):
    conn = connect(client_db_path)
    try:
        tables = [
            tuple(r)
            for r in conn.execute(
                "SELECT line_id, production_day, version FROM instrument_days "
                "ORDER BY line_id, production_day"
            )
        ]
        table_buckets = [
            tuple(r)
            for r in conn.execute(
                "SELECT line_id, production_day, lo, hi, count "
                "FROM instrument_buckets ORDER BY line_id, production_day, position"
            )
        ]
        coils = [
            tuple(r)
            for r in conn.execute(
                "SELECT line_id, production_day, coil_id, lo, hi, count "
                "FROM buckets ORDER BY line_id, production_day, coil_id, position"
            )
        ]
        return tables, table_buckets, coils
    finally:
        conn.close()


def mismatch_body(response):
    body = response.json()
    return body["detail"]["mismatches"]


# --------------------------------------------------------------------------- #
# Pairing primitive
# --------------------------------------------------------------------------- #


def test_pairing_accepts_different_cuts_of_the_same_points():
    instrument = [Bucket(1, 5, 60), Bucket(6, 10, 40)]
    coils = [Bucket(1, 3, 30), Bucket(4, 10, 70)]
    result = pair_buckets(instrument, coils)
    assert result == PairingResult(100, 100, 100)
    assert result.complete


def test_pairing_requires_shared_integer_value():
    # Touching but non-overlapping integer intervals share nothing.
    result = pair_buckets([Bucket(1, 5, 10)], [Bucket(6, 10, 10)])
    assert result.paired == 0
    assert result.instrument_unpaired == 10
    assert result.coil_unpaired == 10


def test_pairing_uses_overlap_not_nesting():
    # Coils are a different, finer cut; partial overlap still pairs.
    result = pair_buckets([Bucket(0, 100, 50)], [Bucket(50, 60, 50)])
    assert result.paired == 50
    assert result.complete


def test_pairing_counts_only_bucket_counts_no_point_expansion():
    # 2 * 10**12 points must return instantly: nothing is expanded.
    instrument = [Bucket(0, 0, 10**12), Bucket(1, 1, 10**12)]
    coils = [Bucket(0, 9, 10**12), Bucket(0, 9, 10**12)]
    result = pair_buckets(instrument, coils)
    assert result.instrument_total == 2 * 10**12
    assert result.paired == 2 * 10**12
    assert result.complete


def test_pairing_extra_points_on_either_side_are_unpaired():
    result = pair_buckets([Bucket(1, 4, 10)], [Bucket(1, 10, 12)])
    assert result.paired == 10
    assert result.instrument_unpaired == 0
    assert result.coil_unpaired == 2


def test_pairing_can_choose_between_overlapping_buckets():
    # Instrument [5,9] is the most constrained and must pair with the coil
    # bucket that also covers [1,5]; a naive earliest-first could waste it.
    instrument = [Bucket(1, 5, 1), Bucket(4, 4, 1), Bucket(4, 9, 1)]
    coils = [Bucket(4, 4, 1), Bucket(5, 9, 1)]
    result = pair_buckets(instrument, coils)
    assert result.paired == 2


def test_pairing_exhaustive_matches_brute_force():
    """Compare the sweep against a brute-force maximum matching on small cases."""

    def brute(instrument, coils):
        left = [(b.lo, b.hi) for b in instrument for _ in range(b.count)]
        right = [(b.lo, b.hi) for b in coils for _ in range(b.count)]
        # maximum bipartite matching via augmenting paths
        adjacency = [
            [j for j, (clo, chi) in enumerate(right) if max(lo, clo) <= min(hi, chi)]
            for lo, hi in left
        ]
        match_right = [-1] * len(right)

        def augment(i, seen):
            for j in adjacency[i]:
                if j in seen:
                    continue
                seen.add(j)
                if match_right[j] == -1 or augment(match_right[j], seen):
                    match_right[j] = i
                    return True
            return False

        return sum(1 for i in range(len(left)) if augment(i, set()))

    rng = random.Random(20261123)
    for _ in range(600):
        def side():
            buckets = []
            for _ in range(rng.randint(1, 3)):
                lo = rng.randint(0, 5)
                buckets.append(Bucket(lo, lo + rng.randint(0, 3), rng.randint(1, 2)))
            return buckets

        instrument, coils = side(), side()
        expected = brute(instrument, coils)
        assert pair_buckets(instrument, coils).paired == expected


# --------------------------------------------------------------------------- #
# Registering and viewing day tables
# --------------------------------------------------------------------------- #


def test_day_table_registered_after_coils_with_different_cut(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 3, "count": 30},
                                 {"lo": 4, "hi": 10, "count": 70}]).status_code == 201
    response = day_table(
        client, buckets=[{"lo": 1, "hi": 5, "count": 60},
                         {"lo": 6, "hi": 10, "count": 40}]
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["buckets"] == [
        {"lo": 1, "hi": 5, "count": 60},
        {"lo": 6, "hi": 10, "count": 40},
    ]
    read = read_table(client)
    assert read.status_code == 200
    assert read.json()["buckets"] == body["buckets"]


def test_day_table_can_be_replaced_and_version_increments(client):
    assert coil(client).status_code == 201
    assert day_table(client).status_code == 201
    replaced = day_table(
        client, buckets=[{"lo": 1, "hi": 4, "count": 40},
                         {"lo": 5, "hi": 10, "count": 60}]
    )
    assert replaced.status_code == 201
    assert replaced.json()["version"] == 2
    assert read_table(client).json()["version"] == 2


def test_day_table_without_coils_is_rejected(client):
    response = day_table(client)
    assert response.status_code == 422
    mismatches = mismatch_body(response)
    assert mismatches == [
        {
            "line_id": "L1",
            "production_day": "2026-11-20",
            "instrument_total": 100,
            "coil_total": 0,
            "instrument_unpaired": 100,
            "coil_unpaired": 0,
        }
    ]
    assert read_table(client).status_code == 404


def test_missing_or_invalid_day_tables_are_404_or_422(client):
    assert read_table(client).status_code == 404
    assert day_table(client, buckets=[]).status_code == 422
    overlapping = day_table(
        client,
        buckets=[{"lo": 1, "hi": 5, "count": 1}, {"lo": 5, "hi": 9, "count": 1}],
    )
    assert overlapping.status_code == 422


def test_day_table_mismatch_reports_both_sides_and_keeps_old_table(client, db_path):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    before = read_table(client).json()

    # Instrument claims 150 points in the same range: 50 cannot pair.
    too_many = day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 150}])
    assert too_many.status_code == 422
    assert mismatch_body(too_many) == [
        {
            "line_id": "L1",
            "production_day": "2026-11-20",
            "instrument_total": 150,
            "coil_total": 100,
            "instrument_unpaired": 50,
            "coil_unpaired": 0,
        }
    ]
    # Previous day table, version and coil report are untouched.
    assert read_table(client).json() == before
    assert client.get(
        "/reports/L1/C1", params={"production_day": "2026-11-20"}
    ).status_code == 200
    tables, _, _ = stored(db_path)
    assert tables == [("L1", "2026-11-20", 1)]


def test_day_table_outside_coil_range_is_rejected(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 5, "count": 10}]).status_code == 201
    response = day_table(client, buckets=[{"lo": 10, "hi": 20, "count": 10}])
    assert response.status_code == 422
    assert mismatch_body(response)[0]["instrument_unpaired"] == 10
    assert mismatch_body(response)[0]["coil_unpaired"] == 10


def test_day_table_aggregates_all_coils_of_the_day(client):
    assert coil(client, coil_id="A", buckets=[{"lo": 1, "hi": 5, "count": 40}]).status_code == 201
    assert coil(client, coil_id="B", buckets=[{"lo": 6, "hi": 10, "count": 60}]).status_code == 201
    accepted = day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}])
    assert accepted.status_code == 201


def test_day_tables_are_independent_across_lines_and_days(client):
    assert coil(client, day="2026-11-20").status_code == 201
    assert coil(client, day="2026-11-21").status_code == 201
    assert coil(client, line="L2", day="2026-11-20").status_code == 201
    assert day_table(
        client, buckets=[{"lo": 1, "hi": 10, "count": 100}]
    ).status_code == 201
    assert day_table(
        client, day="2026-11-21", buckets=[{"lo": 1, "hi": 10, "count": 100}]
    ).status_code == 201
    assert day_table(
        client, line="L2", buckets=[{"lo": 1, "hi": 10, "count": 100}]
    ).status_code == 201


# --------------------------------------------------------------------------- #
# Coil submissions against an existing day table
# --------------------------------------------------------------------------- #


def test_single_coil_submit_must_reconcile_with_whole_day(client, db_path):
    assert coil(client, coil_id="A", buckets=[{"lo": 1, "hi": 5, "count": 50}]).status_code == 201
    assert coil(client, coil_id="B", buckets=[{"lo": 6, "hi": 10, "count": 50}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201

    # Updating A alone keeps the whole day pairing.
    assert coil(client, coil_id="A", buckets=[{"lo": 1, "hi": 4, "count": 50}]).status_code == 201
    # Adding a coil outside the instrument table's range cannot pair.
    rejected = coil(client, coil_id="C", buckets=[{"lo": 20, "hi": 30, "count": 5}])
    assert rejected.status_code == 422
    mismatch = mismatch_body(rejected)[0]
    assert mismatch["line_id"] == "L1"
    assert mismatch["production_day"] == "2026-11-20"
    assert mismatch["coil_unpaired"] == 5
    assert mismatch["instrument_unpaired"] == 0
    # C was not written; the earlier day and p95 remain readable.
    assert client.get(
        "/reports/L1/C", params={"production_day": "2026-11-20"}
    ).status_code == 404
    assert client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()["measurements"] == 100


def test_rejected_coil_update_leaves_previous_report(client, db_path):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    before = client.get(
        "/reports/L1/C1", params={"production_day": "2026-11-20"}
    ).json()

    # Replacing the only coil with a non-overlapping range cannot pair.
    rejected = coil(client, buckets=[{"lo": 50, "hi": 60, "count": 100}])
    assert rejected.status_code == 422
    assert client.get(
        "/reports/L1/C1", params={"production_day": "2026-11-20"}
    ).json() == before


def test_bulk_rejects_entire_batch_when_one_coil_breaks_reconciliation(client, db_path):
    assert coil(client, coil_id="A", buckets=[{"lo": 1, "hi": 5, "count": 50}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 50}]).status_code == 201

    good = {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
            "buckets": [{"lo": 1, "hi": 5, "count": 50}]}
    bad = {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
           "buckets": [{"lo": 100, "hi": 200, "count": 1}]}
    response = bulk(client, [good, bad])
    assert response.status_code == 422
    # Neither the good update nor the bad coil was written.
    assert client.get(
        "/reports/L1/B", params={"production_day": "2026-11-20"}
    ).status_code == 404
    tables, table_buckets, coils = stored(db_path)
    assert tables == [("L1", "2026-11-20", 1)]
    assert table_buckets == [("L1", "2026-11-20", 1, 10, 50)]


def test_bulk_accepts_when_whole_day_still_pairs(client):
    boot = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L1", "production_day": "2026-11-20",
                 "buckets": [{"lo": 1, "hi": 10, "count": 100}]},
            ],
            "reports": [
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
                 "buckets": [{"lo": 1, "hi": 5, "count": 50}]},
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
                 "buckets": [{"lo": 6, "hi": 10, "count": 50}]},
            ],
        },
    )
    assert boot.status_code == 201, boot.text
    # Re-cut both coils; the union still has exactly the 100 instrument points.
    response = bulk(
        client,
        [
            {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
             "buckets": [{"lo": 1, "hi": 3, "count": 50}]},
            {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
             "buckets": [{"lo": 4, "hi": 10, "count": 50}]},
        ],
    )
    assert response.status_code == 201, response.text


def test_days_without_day_table_are_unconstrained(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert coil(client, coil_id="B", buckets=[{"lo": 500, "hi": 900, "count": 7}]).status_code == 201
    assert coil(client, coil_id="C", buckets=[{"lo": 1, "hi": 1, "count": 1}]).status_code == 201


# --------------------------------------------------------------------------- #
# Refinement against an existing day table
# --------------------------------------------------------------------------- #


def test_refinement_that_keeps_pairing_is_accepted(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    response = client.post(
        "/reports/L1/C1/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 10, "count": 100},
            "children": [
                {"lo": 1, "hi": 5, "count": 50},
                {"lo": 6, "hi": 10, "count": 50},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2


def test_refinement_that_breaks_pairing_is_rejected(client, db_path):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    # Instrument only covers the lower half of the coil's declared range.
    assert day_table(client, buckets=[{"lo": 1, "hi": 5, "count": 100}]).status_code == 201
    rejected = client.post(
        "/reports/L1/C1/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 10, "count": 100},
            "children": [
                {"lo": 6, "hi": 10, "count": 100},
            ],
        },
    )
    assert rejected.status_code == 422
    assert mismatch_body(rejected)[0]["coil_unpaired"] == 100
    # Refinement rolled back: original bucket and version intact.
    current = client.get(
        "/reports/L1/C1", params={"production_day": "2026-11-20"}
    ).json()
    assert current["version"] == 1
    assert current["buckets"] == [{"lo": 1, "hi": 10, "count": 100}]


# --------------------------------------------------------------------------- #
# Day table and coil reports submitted together
# --------------------------------------------------------------------------- #


def test_combined_submission_bootstraps_day(client):
    response = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L9", "production_day": "2026-12-01",
                 "buckets": [{"lo": 1, "hi": 4, "count": 40},
                             {"lo": 5, "hi": 4 + 6, "count": 60}]},
            ],
            "reports": [
                {"line_id": "L9", "production_day": "2026-12-01", "coil_id": "A",
                 "buckets": [{"lo": 1, "hi": 5, "count": 50}]},
                {"line_id": "L9", "production_day": "2026-12-01", "coil_id": "B",
                 "buckets": [{"lo": 6, "hi": 10, "count": 50}]},
            ],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["day_tables"][0]["line_id"] == "L9"
    assert {r["coil_id"] for r in body["reports"]} == {"A", "B"}
    assert read_table(client, "L9", "2026-12-01").status_code == 200


def test_combined_submission_that_does_not_pair_is_fully_rejected(client, db_path):
    response = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L9", "production_day": "2026-12-01",
                 "buckets": [{"lo": 1, "hi": 3, "count": 10}]},
            ],
            "reports": [
                {"line_id": "L9", "production_day": "2026-12-01", "coil_id": "A",
                 "buckets": [{"lo": 2, "hi": 5, "count": 6}]},
            ],
        },
    )
    assert response.status_code == 422
    mismatch = mismatch_body(response)[0]
    assert (mismatch["instrument_total"], mismatch["coil_total"]) == (10, 6)
    assert (mismatch["instrument_unpaired"], mismatch["coil_unpaired"]) == (4, 0)
    assert read_table(client, "L9", "2026-12-01").status_code == 404
    assert client.get(
        "/reports/L9/A", params={"production_day": "2026-12-01"}
    ).status_code == 404
    tables, table_buckets, coils = stored(db_path)
    assert tables == [] and table_buckets == [] and coils == []


def test_combined_accepts_day_table_only_when_coils_already_exist(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    response = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L1", "production_day": "2026-11-20",
                 "buckets": [{"lo": 1, "hi": 10, "count": 100}]},
            ],
            "reports": [],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["reports"] == []
    assert read_table(client).status_code == 200


def test_combined_empty_request_is_rejected(client):
    response = client.post("/day-tables/submit", json={"day_tables": [], "reports": []})
    assert response.status_code == 422


def test_combined_with_existing_table_checks_whole_day(client, db_path):
    assert coil(client, coil_id="A", buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201

    # A combined request that replaces the table and adds a coil whose points
    # push the day over the instrument total must be rejected as a whole.
    rejected = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L1", "production_day": "2026-11-20",
                 "buckets": [{"lo": 1, "hi": 10, "count": 100}]},
            ],
            "reports": [
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
                 "buckets": [{"lo": 6, "hi": 10, "count": 10}]},
            ],
        },
    )
    assert rejected.status_code == 422
    # The pre-existing table and coil A are untouched.
    assert read_table(client).json()["buckets"] == [{"lo": 1, "hi": 10, "count": 100}]
    assert client.get(
        "/reports/L1/B", params={"production_day": "2026-11-20"}
    ).status_code == 404

    # Growing the table to cover both coils in one shot is accepted.
    ok = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L1", "production_day": "2026-11-20",
                 "buckets": [{"lo": 1, "hi": 10, "count": 110}]},
            ],
            "reports": [
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
                 "buckets": [{"lo": 6, "hi": 10, "count": 10}]},
            ],
        },
    )
    assert ok.status_code == 201, ok.text


# --------------------------------------------------------------------------- #
# Rejection reports every failing day; readable state survives failures
# --------------------------------------------------------------------------- #


def test_bulk_mismatch_lists_every_failing_line_and_day(client, db_path):
    # Two days have instrument tables; a batch touches both, and both break.
    assert coil(client, line="L1", day="2026-11-20",
                buckets=[{"lo": 1, "hi": 5, "count": 10}]).status_code == 201
    assert coil(client, line="L2", day="2026-11-21",
                buckets=[{"lo": 1, "hi": 5, "count": 20}]).status_code == 201
    assert day_table(client, line="L1", day="2026-11-20",
                     buckets=[{"lo": 1, "hi": 5, "count": 10}]).status_code == 201
    assert day_table(client, line="L2", day="2026-11-21",
                     buckets=[{"lo": 1, "hi": 5, "count": 20}]).status_code == 201

    rejected = bulk(
        client,
        [
            {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "C1",
             "buckets": [{"lo": 100, "hi": 105, "count": 3}]},
            {"line_id": "L2", "production_day": "2026-11-21", "coil_id": "C1",
             "buckets": [{"lo": 1, "hi": 5, "count": 30}]},
        ],
    )
    assert rejected.status_code == 422
    mismatches = {(m["line_id"], m["production_day"]): m
                  for m in mismatch_body(rejected)}
    assert set(mismatches) == {("L1", "2026-11-20"), ("L2", "2026-11-21")}
    assert mismatches[("L2", "2026-11-21")]["coil_unpaired"] == 10
    assert mismatches[("L2", "2026-11-21")]["instrument_unpaired"] == 0
    assert mismatches[("L1", "2026-11-20")]["coil_unpaired"] >= 3
    # Both original coil reports and day tables survive.
    tables, _, coins = stored(db_path)
    assert tables == [("L1", "2026-11-20", 1), ("L2", "2026-11-21", 1)]
    assert len(coins) == 2


def test_p95_and_reports_readable_after_rejection(client):
    assert coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    assert day_table(client, buckets=[{"lo": 1, "hi": 10, "count": 100}]).status_code == 201
    rejected = coil(client, buckets=[{"lo": 1, "hi": 10, "count": 100},
                                     {"lo": 20, "hi": 30, "count": 5}])
    assert rejected.status_code == 422
    p95 = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    )
    assert p95.status_code == 200
    assert p95.json()["measurements"] == 100
    assert read_table(client).status_code == 200
    assert client.get(
        "/reports/L1/C1", params={"production_day": "2026-11-20"}
    ).status_code == 200


def test_mismatch_counts_are_minimum_not_naive_total_difference(client):
    # Instrument: 100 points pinned to [1,2].
    # Coils: 50 points in [1,2] and 50 points in [10,11].
    # A naive "total difference" would say 0 unpaired; in fact 50 points on each
    # side cannot pair because the two ranges share no integer value.
    response = client.post(
        "/day-tables/submit",
        json={
            "day_tables": [
                {"line_id": "L1", "production_day": "2026-11-20",
                 "buckets": [{"lo": 1, "hi": 2, "count": 100}]},
            ],
            "reports": [
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
                 "buckets": [{"lo": 1, "hi": 2, "count": 50}]},
                {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
                 "buckets": [{"lo": 10, "hi": 11, "count": 50}]},
            ],
        },
    )
    assert response.status_code == 422
    mismatch = mismatch_body(response)[0]
    assert (mismatch["instrument_total"], mismatch["coil_total"]) == (100, 100)
    # Instrument keeps 50 unpaired; the 50 far-away coil points cannot pair.
    assert (mismatch["instrument_unpaired"], mismatch["coil_unpaired"]) == (50, 50)

