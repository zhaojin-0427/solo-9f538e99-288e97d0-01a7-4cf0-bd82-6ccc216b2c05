
import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import create_app


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(str(path))
    return str(path)


@pytest.fixture()
def client(db_path):
    app = create_app(db_path)
    with TestClient(app) as test_client:
        yield test_client


def report(client, line="L1", day="2026-11-20", coil="C1", buckets=None):
    if buckets is None:
        buckets = [{"lo": 1, "hi": 5, "count": 10}]
    return client.post(
        "/reports",
        json={
            "line_id": line,
            "production_day": day,
            "coil_id": coil,
            "buckets": buckets,
        },
    )


def rows(db_path):
    conn = connect(db_path)
    try:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT line_id, production_day, coil_id, lo, hi, count "
                "FROM buckets ORDER BY line_id, production_day, coil_id, position"
            ).fetchall()
        ]
    finally:
        conn.close()


def versions(db_path):
    conn = connect(db_path)
    try:
        return {
            (r["line_id"], r["production_day"], r["coil_id"]): r["version"]
            for r in conn.execute(
                "SELECT line_id, production_day, coil_id, version FROM coils"
            ).fetchall()
        }
    finally:
        conn.close()

def test_report_and_read_back(client):
    response = report(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["buckets"] == [{"lo": 1, "hi": 5, "count": 10}]

    read = client.get("/reports/L1/C1", params={"production_day": "2026-11-20"})
    assert read.status_code == 200
    assert read.json()["buckets"] == [{"lo": 1, "hi": 5, "count": 10}]


def test_report_rejects_bad_payloads(client):
    assert report(client, buckets=[]).status_code == 422
    assert report(
        client, buckets=[{"lo": 1, "hi": 5, "count": 1}, {"lo": 4, "hi": 9, "count": 2}]
    ).status_code == 422
    assert report(client, buckets=[{"lo": 5, "hi": 1, "count": 1}]).status_code == 422
    assert report(client, buckets=[{"lo": 1, "hi": 5, "count": 0}]).status_code == 422
    # strict ints: floats, booleans and numeric strings are rejected
    assert report(client, buckets=[{"lo": 1.0, "hi": 5, "count": 1}]).status_code == 422
    assert report(client, buckets=[{"lo": True, "hi": 5, "count": 1}]).status_code == 422
    assert report(client, buckets=[{"lo": 1, "hi": 5, "count": "1"}]).status_code == 422
    assert report(client, day="20-11-2026").status_code == 422
    assert report(client, line="  ").status_code == 422


def test_report_replaces_same_key(client):
    assert report(client).status_code == 201
    replaced = report(client, buckets=[{"lo": 2, "hi": 2, "count": 7}])
    assert replaced.status_code == 201
    assert replaced.json()["version"] == 2
    assert replaced.json()["buckets"] == [{"lo": 2, "hi": 2, "count": 7}]


def test_same_coil_number_on_another_day_is_a_separate_summary(client):
    assert report(client, day="2026-11-20").status_code == 201
    other = report(client, day="2026-11-21", buckets=[{"lo": 3, "hi": 3, "count": 4}])
    assert other.status_code == 201
    assert other.json()["version"] == 1
    assert report(client, day="2026-11-22", coil="C2").status_code == 201


def test_failed_report_keeps_previous_data(client, db_path):
    assert report(client).status_code == 201
    failed = report(
        client,
        buckets=[{"lo": 1, "hi": 5, "count": 3}, {"lo": 5, "hi": 6, "count": 3}],
    )
    assert failed.status_code == 422
    assert rows(db_path) == [("L1", "2026-11-20", "C1", 1, 5, 10)]
    assert versions(db_path) == {("L1", "2026-11-20", "C1"): 1}


def test_bulk_report_is_atomic(client, db_path):
    good = {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
            "buckets": [{"lo": 1, "hi": 2, "count": 3}]}
    bad = {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "B",
           "buckets": [{"lo": 1, "hi": 2, "count": 3}, {"lo": 2, "hi": 4, "count": 1}]}
    response = client.post("/reports/bulk", json={"reports": [good, bad]})
    assert response.status_code == 422
    assert rows(db_path) == []

    ok = client.post(
        "/reports/bulk",
        json={
            "reports": [
                good,
                {**good, "coil_id": "B", "buckets": [{"lo": 5, "hi": 6, "count": 9}]},
            ]
        },
    )
    assert ok.status_code == 201
    assert [item["coil_id"] for item in ok.json()] == ["A", "B"]
    assert rows(db_path) == [
        ("L1", "2026-11-20", "A", 1, 2, 3),
        ("L1", "2026-11-20", "B", 5, 6, 9),
    ]


def test_bulk_rejects_duplicate_key_in_batch(client, db_path):
    item = {"line_id": "L1", "production_day": "2026-11-20", "coil_id": "A",
            "buckets": [{"lo": 1, "hi": 2, "count": 3}]}
    response = client.post("/reports/bulk", json={"reports": [item, item]})
    assert response.status_code == 422
    assert rows(db_path) == []


def test_refinement_applies_and_is_visible(client):
    assert report(client, buckets=[{"lo": 1, "hi": 10, "count": 20}]).status_code == 201
    response = client.post(
        "/reports/L1/C1/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 10, "count": 20},
            "children": [
                {"lo": 1, "hi": 4, "count": 8},
                {"lo": 5, "hi": 10, "count": 12},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["buckets"] == [
        {"lo": 1, "hi": 4, "count": 8},
        {"lo": 5, "hi": 10, "count": 12},
    ]


def test_refinement_target_count_is_optional(client):
    assert report(client, buckets=[{"lo": 1, "hi": 10, "count": 20}]).status_code == 201
    response = client.post(
        "/reports/L1/C1/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 10},
            "children": [
                {"lo": 1, "hi": 4, "count": 8},
                {"lo": 5, "hi": 10, "count": 12},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2


@pytest.mark.parametrize(
    "children, target_count",
    [
        ([{"lo": 1, "hi": 4, "count": 8}, {"lo": 5, "hi": 11, "count": 12}], 20),  # outside
        ([{"lo": 1, "hi": 6, "count": 8}, {"lo": 5, "hi": 10, "count": 12}], 20),  # overlap
        ([{"lo": 1, "hi": 4, "count": 8}, {"lo": 5, "hi": 10, "count": 11}], 20),  # sum
        ([{"lo": 1, "hi": 4, "count": 0}], 20),  # non-positive child count
        ([{"lo": 1, "hi": 4, "count": 8}, {"lo": 5, "hi": 10, "count": 12}], 19),  # stale target
    ],
)
def test_refinement_failures_roll_back(client, db_path, children, target_count):
    assert report(client, buckets=[{"lo": 1, "hi": 10, "count": 20}]).status_code == 201
    response = client.post(
        "/reports/L1/C1/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 10, "count": target_count},
            "children": children,
        },
    )
    assert response.status_code == 422
    assert rows(db_path) == [("L1", "2026-11-20", "C1", 1, 10, 20)]
    assert versions(db_path) == {("L1", "2026-11-20", "C1"): 1}


def test_refinement_of_unknown_or_wrong_day_rejected(client, db_path):
    assert report(client, buckets=[{"lo": 1, "hi": 10, "count": 20}]).status_code == 201
    base = {
        "children": [{"lo": 1, "hi": 5, "count": 10}, {"lo": 6, "hi": 10, "count": 10}],
    }
    wrong_interval = client.post(
        "/reports/L1/C1/refinements",
        json={**base, "production_day": "2026-11-20", "target": {"lo": 2, "hi": 10}},
    )
    assert wrong_interval.status_code == 422
    wrong_day = client.post(
        "/reports/L1/C1/refinements",
        json={**base, "production_day": "2026-11-19", "target": {"lo": 1, "hi": 10}},
    )
    assert wrong_day.status_code == 422
    unknown = client.post(
        "/reports/L1/C9/refinements",
        json={**base, "production_day": "2026-11-20", "target": {"lo": 1, "hi": 10}},
    )
    assert unknown.status_code == 422
    assert rows(db_path) == [("L1", "2026-11-20", "C1", 1, 10, 20)]


def test_p95_without_data(client):
    response = client.get(
        "/p95", params={"line_id": "none", "start": "2026-11-01", "end": "2026-11-30"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_data"] is False
    assert body["p95_min"] is None and body["p95_max"] is None
    assert body["candidate_buckets"] == []


def test_p95_range_and_candidates(client):
    # Coil A: 10 readings at exactly 10. Coil B: 10 in [10,20].
    # n=20, rank 19 -> min 10, max 20.
    report(client, coil="A", buckets=[{"lo": 10, "hi": 10, "count": 10}])
    report(client, coil="B", buckets=[{"lo": 10, "hi": 20, "count": 10}])
    body = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert body["has_data"] is True
    assert body["measurements"] == 20
    assert body["rank"] == 19
    assert (body["p95_min"], body["p95_max"]) == (10, 20)
    # A=[10,10] overlaps [10,20] in one value only -> excluded (needs >= 2).
    # B=[10,20] overlaps in 11 values -> included.
    assert [(c["coil_id"], c["lo"], c["hi"]) for c in body["candidate_buckets"]] == [
        ("B", 10, 20)
    ]
    assert body["coil_ids"] == ["B"]


def test_p95_degenerate_has_no_candidate_list(client):
    report(client, coil="A", buckets=[{"lo": 10, "hi": 10, "count": 100}])
    body = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert body["p95_min"] == body["p95_max"] == 10
    assert body["candidate_buckets"] == []


def test_p95_day_range_filters_and_validates(client):
    report(client, day="2026-11-19", coil="A", buckets=[{"lo": 1, "hi": 1, "count": 100}])
    report(client, day="2026-11-20", coil="A", buckets=[{"lo": 50, "hi": 50, "count": 100}])
    only_first = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-19", "end": "2026-11-19"}
    ).json()
    assert only_first["p95_min"] == 1
    both = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-19", "end": "2026-11-20"}
    ).json()
    assert (both["p95_min"], both["p95_max"]) == (50, 50)
    inverted = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-19"}
    )
    assert inverted.status_code == 422
    other_line = client.get(
        "/p95", params={"line_id": "L2", "start": "2026-11-19", "end": "2026-11-20"}
    ).json()
    assert other_line["has_data"] is False


def test_refinement_narrows_p95_candidates(client):
    report(client, coil="A", buckets=[{"lo": 1, "hi": 100, "count": 100}])
    coarse = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert (coarse["p95_min"], coarse["p95_max"]) == (1, 100)
    assert coarse["candidate_buckets"][0]["coil_id"] == "A"

    refined = client.post(
        "/reports/L1/A/refinements",
        json={
            "production_day": "2026-11-20",
            "target": {"lo": 1, "hi": 100, "count": 100},
            "children": [
                {"lo": 1, "hi": 50, "count": 50},
                {"lo": 51, "hi": 100, "count": 50},
            ],
        },
    )
    assert refined.status_code == 200
    narrower = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert narrower["p95_min"] == 51
    assert narrower["candidate_buckets"] == [
        {"coil_id": "A", "production_day": "2026-11-20", "lo": 51, "hi": 100, "count": 50}
    ]


def test_p95_reads_one_snapshot(client, db_path, monkeypatch):
    """The bounds and candidate list must come from one committed submission."""
    report(client, coil="A", buckets=[{"lo": 1, "hi": 100, "count": 100}])

    from app import db, service

    writes = []
    original_rank = service.p95_rank

    def rank_then_write(total):
        rank = original_rank(total)
        if not writes:
            writes.append(1)
            # A concurrent refinement commits from another connection after
            # the snapshot has started reading; the query must not see it.
            other = db.connect(db_path)
            try:
                with db.transaction(other):
                    other.execute(
                        "DELETE FROM buckets "
                        "WHERE line_id = 'L1' AND production_day = '2026-11-20' "
                        "AND coil_id = 'A'"
                    )
                    other.execute(
                        "INSERT INTO buckets (line_id, production_day, coil_id, position, "
                        "lo, hi, count) VALUES ('L1','2026-11-20','A',0,1,50,50)"
                    )
            finally:
                other.close()
        return rank

    monkeypatch.setattr(service, "p95_rank", rank_then_write)
    body = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert (body["p95_min"], body["p95_max"]) == (1, 100)
    assert len(body["candidate_buckets"]) == 1
    assert writes == [1]

    # The next query sees the newer committed state: one bucket [1,50] x50,
    # so its own bounds are 1 and 50 again.
    fresh = client.get(
        "/p95", params={"line_id": "L1", "start": "2026-11-20", "end": "2026-11-20"}
    ).json()
    assert (fresh["p95_min"], fresh["p95_max"]) == (1, 50)


def test_missing_coil_is_404(client):
    assert client.get(
        "/reports/L1/missing", params={"production_day": "2026-11-20"}
    ).status_code == 404


def test_docs_and_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    openapi = client.get("/openapi.json").json()
    assert "/p95" in openapi["paths"]
    assert "/reports/bulk" in openapi["paths"]


def test_out_of_range_count_is_422_not_500(client):
    from app.domain import MAX_COUNT

    response = report(
        client, buckets=[{"lo": 1, "hi": 2, "count": MAX_COUNT + 1}]
    )
    assert response.status_code == 422
    response = report(client, buckets=[{"lo": 1, "hi": 2, "count": 10**30}])
    assert response.status_code == 422
