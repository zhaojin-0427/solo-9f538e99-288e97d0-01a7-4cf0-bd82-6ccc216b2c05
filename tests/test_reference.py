"""Cross-check the SQL aggregation against the pure-Python reference."""

from __future__ import annotations

import random
from datetime import date, timedelta

from fastapi.testclient import TestClient

from app.db import init_db
from app.domain import Bucket, overlap_values, p95_range
from app.main import create_app


def random_buckets(rng: random.Random) -> list[dict]:
    buckets = []
    lo = 0
    for _ in range(rng.randint(1, 6)):
        hi = lo + rng.randint(0, 4)
        buckets.append({"lo": lo, "hi": hi, "count": rng.randint(1, 30)})
        lo = hi + 1 + rng.randint(0, 2)
    return buckets


def test_sql_p95_matches_reference(tmp_path):
    rng = random.Random(20261122)
    app = create_app(str(tmp_path / "ref.db"))
    with TestClient(app) as client:
        expected: list[Bucket] = []
        for day_offset in range(4):
            day = date(2026, 11, 18) + timedelta(days=day_offset)
            for coil_index in range(3):
                buckets = random_buckets(rng)
                response = client.post(
                    "/reports",
                    json={
                        "line_id": "L",
                        "production_day": day.isoformat(),
                        "coil_id": f"C{coil_index}",
                        "buckets": buckets,
                    },
                )
                assert response.status_code == 201, response.text
                expected.extend(Bucket(**b) for b in buckets)

        reference = p95_range(expected)
        body = client.get(
            "/p95",
            params={"line_id": "L", "start": "2026-11-18", "end": "2026-11-21"},
        ).json()

        assert body["measurements"] == reference.total
        assert body["rank"] == reference.rank
        assert (body["p95_min"], body["p95_max"]) == (
            reference.minimum,
            reference.maximum,
        )

        expected_candidates = {
            (b.lo, b.hi, b.count)
            for b in expected
            if overlap_values(b, reference.minimum, reference.maximum) >= 2
        }
        actual_candidates = {
            (c["lo"], c["hi"], c["count"]) for c in body["candidate_buckets"]
        }
        assert actual_candidates == expected_candidates


def test_p95_matches_reference_many_trials(tmp_path):
    rng = random.Random(7)
    path = str(tmp_path / "many.db")
    init_db(path)
    app = create_app(path)
    with TestClient(app) as client:
        accumulated: list[Bucket] = []
        for trial in range(12):
            buckets = random_buckets(rng)
            client.post(
                "/reports",
                json={
                    "line_id": "L",
                    "production_day": f"2026-12-{trial + 1:02d}",
                    "coil_id": "X",
                    "buckets": buckets,
                },
            )
            accumulated.extend(Bucket(**b) for b in buckets)
            body = client.get(
                "/p95",
                params={
                    "line_id": "L",
                    "start": "2026-12-01",
                    "end": f"2026-12-{trial + 1:02d}",
                },
            ).json()
            reference = p95_range(accumulated)
            assert body["rank"] == reference.rank
            assert body["measurements"] == reference.total
            assert (body["p95_min"], body["p95_max"]) == (
                reference.minimum,
                reference.maximum,
            )
