"""GET /v1/derived: every derived series with its latest point, in one request.

The derived hub page used to make one call per series (56 with the catalog). Cloudflare's free plan
lets the website's Worker make 50 per page, so the last seven series rendered blank and the page
could never be stored at the edge (2026-11-14).
"""
from __future__ import annotations

from datetime import date

import pytest

from tests.test_api_integration import _db_available, api_key, client  # noqa: F401 — fixtures

pytestmark = pytest.mark.skipif(not _db_available(), reason="no database")

IDS = ("pytest.derived.alpha", "pytest.derived.beta", "pytest.derived.empty")


@pytest.fixture
def derived_rows():
    from app import db
    from app.security import get_redis

    for sid in IDS:
        db.execute(
            "INSERT INTO series (id, module, name, unit, frequency, source, tier, is_active, derivation) "
            "VALUES (%s,'economic',%s,'percent','monthly','PakDataHub','basic',true,"
            "'{\"method\": \"a - b\", \"inputs\": [\"x\", \"y\"], \"version\": 2}'::jsonb) "
            "ON CONFLICT (id) DO NOTHING", (sid, f"Pytest {sid}"))
    for d, v in ((date(2026, 8, 1), 1.5), (date(2026, 9, 1), 2.25), (date(2026, 7, 1), 0.5)):
        db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES (%s,%s,%s,'{}')",
                   (IDS[0], d, v))
    db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES (%s,'2025-12-01',-3,'{}')",
               (IDS[1],))
    get_redis().delete("cache:v1:derived:hub")
    yield
    db.execute("DELETE FROM observations WHERE series_id = ANY(%s)", (list(IDS),))
    db.execute("DELETE FROM series WHERE id = ANY(%s)", (list(IDS),))
    get_redis().delete("cache:v1:derived:hub")


def test_derived_needs_a_key(client):  # noqa: F811
    assert client.get("/v1/derived").status_code == 401


def test_derived_hub_in_one_call(client, api_key, derived_rows):  # noqa: F811
    r = client.get("/v1/derived", headers={"X-API-Key": api_key})
    assert r.status_code == 200
    body = r.json()
    by_id = {d["id"]: d for d in body["data"]}
    assert set(IDS) <= set(by_id)
    assert all(d["source"] == "PakDataHub" for d in body["data"])
    assert by_id[IDS[0]]["latest"] == {"date": "2026-09-01", "value": 2.25}  # the newest, not the last inserted
    assert by_id[IDS[1]]["latest"] == {"date": "2025-12-01", "value": -3.0}
    assert by_id[IDS[2]]["latest"] is None  # no observations yet
    assert by_id[IDS[0]]["derived"] == {"method": "a - b", "inputs": ["x", "y"], "version": 2}
    assert body["pagination"]["count"] == len(body["data"])
