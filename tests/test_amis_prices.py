"""Punjab AMIS wholesale prices — parsers on real pages, upsert semantics, API."""
from datetime import date
from pathlib import Path

import pytest

from ingestion.jobs.amis_prices import (MAJOR_MARKETS, parse_day_page, parse_listbox, parse_trends_csv, slug,
                                        unit_for)

from tests.test_api_integration import _db_available, api_key, client  # noqa: F401 — shared fixtures

FIX = Path(__file__).parent / "fixtures"
needs_db = pytest.mark.skipif(not _db_available(), reason="no database reachable")


def test_client_retries_a_stalled_request(monkeypatch):
    """2026-10-16/18/20: AMIS's first page hung past the timeout and sank the whole run;
    the next request answers in ~1 s."""
    import httpx

    from ingestion.jobs import amis_prices

    monkeypatch.setattr(amis_prices.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, text="ok")

    cl = amis_prices.AmisClient()
    cl.c = httpx.Client(transport=httpx.MockTransport(handler))
    assert cl._get("http://amis.test/x") == "ok" and len(calls) == 2

    def forbidden(request):
        calls.append(request.url)
        return httpx.Response(404)

    cl.c = httpx.Client(transport=httpx.MockTransport(forbidden))
    calls.clear()
    with pytest.raises(httpx.HTTPStatusError):
        cl._get("http://amis.test/x")
    assert len(calls) == 1  # a 4xx is not retried


def test_market_day_page_rows_with_category():
    when, rows = parse_day_page((FIX / "amis_city_faisalabad_20261002.html").read_text(encoding="utf-8"))
    assert when == date(2026, 10, 2)
    wheat = next(r for r in rows if r["commodity_id"] == 1)
    assert wheat == {"commodity_id": 1, "market_id": 2, "commodity": "Wheat", "category": "Grains",
                     "min": 11500.0, "max": 12000.0, "fqp": 11750.0, "quantity": None}
    assert {r["category"] for r in rows} == {"Grains", "Vegetables", "Fruits"}
    assert all(r["market_id"] == 2 for r in rows)
    assert all(r["fqp"] is None or r["fqp"] > 0 for r in rows)  # "-" cells are skipped, never 0


def test_commodity_page_lists_markets():
    page = (FIX / "amis_commodity_wheat.html").read_text(encoding="utf-8")
    _when, rows = parse_day_page(page)
    assert rows and all(r["commodity_id"] == 1 for r in rows)
    assert len(parse_listbox(page)) == 143


def test_trends_csv_is_fqp_by_day():
    pts = parse_trends_csv((FIX / "amis_trends_wheat_faisalabad.csv").read_text(encoding="utf-8-sig"))
    assert pts[-1] == (date(2026, 10, 5), 11750.0)
    assert all(v > 0 for _d, v in pts)


def test_slug_unit_and_major_markets():
    assert slug("Bitter Gourd (کریلا)") == "bitter_gourd"
    assert unit_for("Banana(DOZEN)") == "pkr_per_dozen" and unit_for("Onion") == "pkr_per_100kg"
    assert "lahore" in {slug(m) for m in MAJOR_MARKETS}


@pytest.fixture
def agri_rows():
    from app import db
    from ingestion.jobs.amis_prices import upsert_prices, upsert_reference

    db.execute("DELETE FROM agri_prices WHERE commodity_id IN (990001) ")
    upsert_reference({990001: "Testmandi"}, {990001: ("Test Onion", "Vegetables")})
    yield upsert_prices
    db.execute("DELETE FROM agri_prices WHERE commodity_id = 990001")
    db.execute("DELETE FROM agri_commodities WHERE commodity_id = 990001")
    db.execute("DELETE FROM agri_markets WHERE market_id = 990001")


@needs_db
def test_fqp_only_backfill_never_erases_min_max(agri_rows):
    from app import db

    upsert = agri_rows
    d = date(2026, 9, 1)
    upsert([{"commodity_id": 990001, "market_id": 990001, "obs_date": d, "min": 100, "max": 140, "fqp": 120,
             "quantity": None}])
    upsert([{"commodity_id": 990001, "market_id": 990001, "obs_date": d, "min": None, "max": None, "fqp": 121,
             "quantity": None}])
    row = db.query_one("SELECT min_price, max_price, fqp FROM agri_prices WHERE commodity_id = 990001")
    assert (float(row["min_price"]), float(row["max_price"]), float(row["fqp"])) == (100, 140, 121)


@needs_db
def test_agri_api(client, api_key, agri_rows):
    upsert = agri_rows
    upsert([{"commodity_id": 990001, "market_id": 990001, "obs_date": date(2026, 9, d), "min": 100 + d,
             "max": 140 + d, "fqp": 120 + d, "quantity": None} for d in (1, 2, 3)])
    assert client.get("/v1/agri/markets").status_code == 200
    r = client.get("/v1/agri/prices", params={"commodity": "test_onion", "market": "testmandi", "sort": "asc"},
                   headers={"X-API-Key": api_key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["fqp"] for x in body["data"]] == [121.0, 122.0, 123.0]
    assert body["commodity"]["unit"] == "pkr_per_100kg" and body["data"][0]["min"] == 101.0
    csv = client.get("/v1/agri/prices", params={"commodity": "990001", "format": "csv"}, headers={"X-API-Key": api_key})
    assert csv.text.splitlines()[0] == "date,market,commodity,min,max,fqp,quantity,unit"
    assert client.get("/v1/agri/prices", params={"commodity": "nope"}, headers={"X-API-Key": api_key}).status_code == 404
    assert client.get("/v1/agri/prices", params={"commodity": "test_onion"}).status_code == 401


@needs_db
def test_agri_latest_by_market_or_commodity(client, api_key, agri_rows):
    from datetime import timedelta

    from app import db

    upsert = agri_rows
    today = date.today()
    upsert([{"commodity_id": 990001, "market_id": 990001, "obs_date": today - timedelta(days=k), "min": None,
             "max": None, "fqp": 200 - k, "quantity": None} for k in (1, 2)])
    db.execute("DELETE FROM agri_prices WHERE commodity_id = 990001 AND market_id = 990001 AND obs_date < %s",
               (today - timedelta(days=5),))
    from app.security import get_redis
    get_redis().delete("cache:v1:agri:latest:v2:-:990001:-", "cache:v1:agri:latest:v2:990001:990001:-")
    h = {"X-API-Key": api_key}
    by_market = client.get("/v1/agri/latest", params={"market": "testmandi"}, headers=h).json()
    assert by_market["market"]["slug"] == "testmandi"
    assert [(x["commodity"], x["fqp"]) for x in by_market["data"]] == [("Test Onion", 199.0)]
    both = client.get("/v1/agri/latest", params={"market": "990001", "commodity": "test_onion"}, headers=h).json()
    assert both["data"][0]["fqp"] == 199.0 and both["commodity"]["slug"] == "test_onion"
    assert client.get("/v1/agri/latest", headers=h).status_code == 422


@needs_db
def test_agri_crop_spans_both_provinces_in_rs_per_kg(client, api_key, agri_rows):
    """crop=<punjab slug> returns Punjab's commodity and Sindh's graded twins, each with fqp_per_kg."""
    from datetime import timedelta

    from app import db
    from app.security import get_redis
    from ingestion.agri_names import apply_names

    upsert = agri_rows
    today = date.today() - timedelta(days=1)
    db.execute("""INSERT INTO agri_markets (market_id, name, slug, is_major, province, district)
                  VALUES (999901, 'Testabad Market', 'sindh_testabad', false, 'Sindh', 'Karachi (Division)')
                  ON CONFLICT (market_id) DO NOTHING""")
    db.execute("""INSERT INTO agri_commodities (commodity_id, name, slug, category, unit, province)
                  VALUES (999901, 'Test Onion, Grade 1', 'sindh_test_onion_grade_1', 'Vegetables', 'pkr_per_kg', 'Sindh')
                  ON CONFLICT (commodity_id) DO NOTHING""")
    try:
        with db.connection() as conn:
            apply_names(conn)
        db.execute("UPDATE agri_commodities SET crop_key = 'test_onion' WHERE commodity_id = 999901")
        upsert([{"commodity_id": 990001, "market_id": 990001, "obs_date": today, "min": None, "max": None,
                 "fqp": 15000, "quantity": None},
                {"commodity_id": 999901, "market_id": 999901, "obs_date": today, "min": None, "max": None,
                 "fqp": 160, "quantity": None}])
        get_redis().delete(*[k for k in get_redis().scan_iter("cache:v1:agri:*")])
        body = client.get("/v1/agri/latest", params={"crop": "test_onion"}, headers={"X-API-Key": api_key}).json()
        got = {(x["province"], x["market"], x["fqp_per_kg"]) for x in body["data"]}
        assert got == {("Punjab", "Testmandi", 150.0), ("Sindh", "Testabad", 160.0)}
        assert set(body["sources"]) == {"Punjab", "Sindh"}
        only = client.get("/v1/agri/latest", params={"crop": "test_onion", "province": "Sindh"},
                          headers={"X-API-Key": api_key}).json()
        assert {x["province"] for x in only["data"]} == {"Sindh"}
        markets = {m["slug"]: m for m in client.get("/v1/agri/markets").json()["data"]}
        assert markets["sindh_testabad"]["name"] == "Testabad" and markets["sindh_testabad"]["region"] == "Sindh"
    finally:
        db.execute("DELETE FROM agri_prices WHERE commodity_id = 999901")
        db.execute("DELETE FROM agri_commodities WHERE commodity_id = 999901")
        db.execute("DELETE FROM agri_markets WHERE market_id = 999901")
        get_redis().delete(*([k for k in get_redis().scan_iter("cache:v1:agri:*")] or ["_"]))
