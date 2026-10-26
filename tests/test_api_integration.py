"""End-to-end API tests against a live database.

Every other test file is a pure parser unit test. These exercise the actual
serving path - routing, auth, tier gating, query layer and JSON envelope - which
was previously only ever checked by hand with curl.

Skipped automatically when no database is reachable, so `pytest` still runs
offline; run them inside the stack:

    docker compose exec api python -m pytest tests/test_api_integration.py
"""
from __future__ import annotations

import os
import uuid

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


def _db_available() -> bool:
    try:
        from app import db

        db.query_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no database reachable")


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return fastapi_testclient.TestClient(app)


@pytest.fixture(scope="module")
def api_key() -> str:
    """A pro key so tier/history gating never masks a real failure."""
    from app import db
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    row = db.query_one(
        "INSERT INTO users (email, plan, subscription_status) "
        "VALUES (%s,'pro','active') RETURNING id",
        (f"pytest-{uuid.uuid4().hex[:12]}@pakdata.local",),
    )
    db.execute(
        "INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'pytest')",
        (key_hash, row["id"]),
    )
    yield plaintext
    db.execute("DELETE FROM api_keys WHERE key_hash=%s", (key_hash,))
    db.execute("DELETE FROM users WHERE id=%s", (row["id"],))


@pytest.fixture(scope="module", autouse=True)
def _minimal_data():
    """Seed the rows these tests read when the DB doesn't already have them (a fresh CI
    database after `db.init`): a CPI index history, one fund with NAV/returns/TER, one
    sukuk. Anything inserted here is removed afterwards; a populated dev DB is untouched."""
    from datetime import date

    from app import db

    added: list[tuple[str, tuple]] = []
    if not db.query_one("SELECT 1 AS x FROM observations WHERE series_id='inflation.cpi.national' LIMIT 1"):
        db.execute(
            "INSERT INTO series (id, module, name, unit, frequency, source, tier, is_active) "
            "VALUES ('inflation.cpi.national','prices','CPI National','index','monthly','PBS','basic',true) "
            "ON CONFLICT (id) DO NOTHING")
        months = [date(2015 + m // 12, m % 12 + 1, 1) for m in range(140)]   # 2015-01 .. 2026-08
        for i, d in enumerate(months):
            db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES (%s,%s,%s,'{}')",
                       ("inflation.cpi.national", d, round(101 * 1.006 ** i, 2)))
        added.append(("DELETE FROM observations WHERE series_id=%s", ("inflation.cpi.national",)))
    if not db.query_one("SELECT 1 AS x FROM funds f JOIN fund_navs n USING (fund_id) WHERE f.is_active LIMIT 1"):
        fid = 990001
        db.execute("INSERT INTO funds (fund_id, name, amc, sector, category) VALUES "
                   "(%s,'Pytest Income Fund','Pytest AMC','Open-End Funds','Income')", (fid,))
        for day in (1, 2, 3):
            db.execute("INSERT INTO fund_navs (fund_id, obs_date, nav) VALUES (%s,%s,%s)",
                       (fid, date(2026, 10, day), 100 + day / 10))
        db.execute("INSERT INTO fund_returns (fund_id, obs_date, ytd, d90, d365) VALUES (%s,'2026-10-03',9.1,10.2,12.3)", (fid,))
        db.execute("INSERT INTO fund_expenses (fund_id, obs_date, ter_ytd) VALUES (%s,'2026-10-03',1.5)", (fid,))
        for table in ("fund_expenses", "fund_returns", "fund_navs", "funds"):
            added.append((f"DELETE FROM {table} WHERE fund_id=%s", (fid,)))
    if not db.query_one("SELECT 1 AS x FROM securities WHERE security_type='gis_sukuk' LIMIT 1"):
        db.execute("INSERT INTO securities (code, security_type) VALUES ('GOPIS-VRR-2030-01-15','gis_sukuk')")
        added.append(("DELETE FROM securities WHERE code=%s", ("GOPIS-VRR-2030-01-15",)))
    yield
    for sql, params in added:
        db.execute(sql, params)


def _auth(key: str) -> dict:
    return {"X-API-Key": key}


# ---- public endpoints --------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_root_is_not_a_404(client):
    """The bare host used to 404: browsers now go to the docs, API clients get links."""
    r = client.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://pakdatahub.com/docs"
    for path in ("/", "/v1"):
        body = client.get(path, headers={"Accept": "application/json"}).json()
        assert body["success"] and body["docs"].startswith("https://")


def test_status_reports_series_and_dedicated_datasets(client):
    """Regression: /v1/status used to see only the series store, so funds,
    securities and trade - the bulk of the product - were invisible on the
    public trust page."""
    r = client.get("/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert {"modules", "datasets", "jobs"} <= set(body)
    names = {d["dataset"] for d in body["datasets"]}
    assert {"funds", "securities", "trade", "auctions"} <= names


def test_catalog_is_public_and_shaped(client):
    r = client.get("/v1/catalog", params={"module": "economic"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True and isinstance(body["data"], list)
    if body["data"]:
        assert {"id", "module", "name", "frequency", "source"} <= set(body["data"][0])


def test_commodity_enums_are_public(client):
    for path in ("/v1/commodities/items", "/v1/commodities/cities"):
        r = client.get(path)
        assert r.status_code == 200 and r.json()["success"] is True


# ---- auth --------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/v1/series/inflation.cpi.national",
    "/v1/funds",
    "/v1/securities",
    "/v1/trade",
    "/v1/fixed-income/auctions",
])
def test_protected_endpoints_require_a_key(client, path):
    assert client.get(path).status_code == 401


def test_invalid_key_rejected(client):
    r = client.get("/v1/series/inflation.cpi.national", headers=_auth("pk_live_nope"))
    assert r.status_code == 401
    assert r.json()["success"] is False


# ---- series ------------------------------------------------------------------

def test_series_envelope_and_values(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national", headers=_auth(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["series"] == "inflation.cpi.national"
    assert {"name", "unit", "frequency", "source"} <= set(body["meta"])
    assert body["data"], "expected CPI observations"
    point = body["data"][0]
    assert {"date", "value", "dims"} <= set(point)
    assert point["value"] > 100  # an index level, never an inflation rate


def test_series_date_window_and_limit(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national",
                   params={"from": "2020-01-01", "to": "2020-12-31", "sort": "asc"},
                   headers=_auth(api_key))
    assert r.status_code == 200
    dates = [d["date"] for d in r.json()["data"]]
    assert dates == sorted(dates)
    assert all(d.startswith("2020") for d in dates)

    r2 = client.get("/v1/series/inflation.cpi.national", params={"limit": 3},
                    headers=_auth(api_key))
    assert len(r2.json()["data"]) <= 3


def test_series_csv_export(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national",
                   params={"format": "csv", "limit": 2}, headers=_auth(api_key))
    assert r.status_code == 200
    assert r.text.splitlines()[0] == "series,date,value,dims"


def test_latest_honours_dims_and_csv(client, api_key):
    """/latest used to ignore dims (returned bid AND offer for side:offer) and format."""
    sid = "rates.kibor.3m"
    r = client.get(f"/v1/series/{sid}/latest", headers=_auth(api_key))
    if r.status_code != 200 or not r.json()["data"]:
        pytest.skip("no KIBOR data in this database")
    sides = {d["dims"].get("side") for d in r.json()["data"]}
    r = client.get(f"/v1/series/{sid}/latest", params={"dims": "side:offer"}, headers=_auth(api_key))
    assert {d["dims"].get("side") for d in r.json()["data"]} <= {"offer"}
    if "offer" in sides:
        assert r.json()["data"]
    r = client.get(f"/v1/series/{sid}/latest", params={"dims": "side:offer", "format": "csv"},
                   headers=_auth(api_key))
    lines = r.text.splitlines()
    assert lines[0] == "series,date,value,dims"
    # dims is compact JSON (was a Python repr like {'side': 'offer'})
    assert all('{""side"":""offer""}' in ln for ln in lines[1:])


def test_unknown_series_is_404_not_500(client, api_key):
    r = client.get("/v1/series/does.not.exist", headers=_auth(api_key))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


# ---- server-side transforms --------------------------------------------------

def test_series_transform_yoy_echoes_meta_and_reunits(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national",
                   params={"transform": "yoy", "from": "2019-01-01", "sort": "asc"},
                   headers=_auth(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["transform"] == "yoy"
    assert body["meta"]["unit"] == "percent"
    # a yoy inflation rate is a small percent, never the index level (>100)
    vals = [d["value"] for d in body["data"] if d["value"] is not None]
    assert vals and all(-50 < v < 100 for v in vals)


def test_series_transform_index_starts_at_100(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national",
                   params={"transform": "index", "from": "2019-01-01", "sort": "asc"},
                   headers=_auth(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["meta"]["unit"] == "index"
    first = next(d["value"] for d in body["data"] if d["value"] is not None)
    assert first == pytest.approx(100.0)


def test_series_transform_invalid_is_422(client, api_key):
    r = client.get("/v1/series/inflation.cpi.national",
                   params={"transform": "bogus"}, headers=_auth(api_key))
    assert r.status_code == 422


# ---- account profile + key last-used -----------------------------------------

def test_account_profile_update_is_partial(client, api_key):
    # set both
    r = client.patch("/v1/account", json={"name": "Ada Lovelace", "company": "Analytical Engines"},
                      headers=_auth(api_key))
    assert r.status_code == 200
    acct = client.get("/v1/account", headers=_auth(api_key)).json()
    assert acct["name"] == "Ada Lovelace" and acct["company"] == "Analytical Engines"

    # patch only name -> company is preserved (partial update)
    client.patch("/v1/account", json={"name": "Ada"}, headers=_auth(api_key))
    acct = client.get("/v1/account", headers=_auth(api_key)).json()
    assert acct["name"] == "Ada" and acct["company"] == "Analytical Engines"

    # empty string clears
    client.patch("/v1/account", json={"company": ""}, headers=_auth(api_key))
    acct = client.get("/v1/account", headers=_auth(api_key)).json()
    assert acct["company"] is None


def test_key_last_used_is_recorded(client, api_key):
    # authenticating with the key at least once records last_used_at
    client.get("/v1/account", headers=_auth(api_key))
    keys = client.get("/v1/keys", headers=_auth(api_key)).json()["data"]
    assert keys and keys[0].get("last_used_at") is not None


# ---- revision vintages -------------------------------------------------------

def test_vintage_returns_value_as_of_date(client, api_key):
    """The trigger records the prior value on revision; ?vintage= reconstructs
    the value that was current on a past date."""
    from app import db

    sid = "test.vintage.demo"
    db.execute(
        "INSERT INTO series (id, module, name, frequency, source, tier, is_active) "
        "VALUES (%s,'prices','Vintage Test','monthly','TEST','basic',true) "
        "ON CONFLICT (id) DO NOTHING",
        (sid,),
    )
    db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
    db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
    try:
        # value 100 first published, later revised to 110 (trigger logs prior 100)
        db.execute(
            "INSERT INTO observations (series_id, obs_date, value, dims, revised_at) "
            "VALUES (%s,'2026-01-31',100,'{}','2026-02-05')",
            (sid,),
        )
        db.execute("UPDATE observations SET value=110 WHERE series_id=%s", (sid,))
        # pin timestamps so the as-of boundaries are deterministic
        db.execute("UPDATE observations SET revised_at='2026-06-01' WHERE series_id=%s", (sid,))
        db.execute("UPDATE observation_revisions SET superseded_at='2026-06-01' WHERE series_id=%s", (sid,))

        # the trigger actually recorded the prior value
        rev = db.query_one("SELECT value FROM observation_revisions WHERE series_id=%s", (sid,))
        assert rev and float(rev["value"]) == 100.0

        # as of the day before 110 became current -> the old 100
        r = client.get(f"/v1/series/{sid}", params={"vintage": "2026-05-31"}, headers=_auth(api_key))
        assert r.status_code == 200 and r.json()["data"][0]["value"] == 100.0
        # as of the day 110 became current -> 110
        r2 = client.get(f"/v1/series/{sid}", params={"vintage": "2026-06-01"}, headers=_auth(api_key))
        assert r2.json()["data"][0]["value"] == 110.0
        # no vintage -> current value
        r3 = client.get(f"/v1/series/{sid}", headers=_auth(api_key))
        assert r3.json()["data"][0]["value"] == 110.0
    finally:
        db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM series WHERE id=%s", (sid,))


def test_vintage_survives_unchanged_reingest(client, api_key):
    """Regression (2026-10-04): every ingest re-upserts history; that used to bump
    revised_at, so ?vintage= for any earlier date returned nothing. An unchanged
    re-upsert must keep revised_at/first_seen_at, and the point stays in old vintages."""
    from app import db

    sid = "test.vintage.reingest"
    db.execute(
        "INSERT INTO series (id, module, name, frequency, source, tier, is_active) "
        "VALUES (%s,'prices','Vintage Reingest','monthly','TEST','basic',true) ON CONFLICT (id) DO NOTHING",
        (sid,),
    )
    db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
    db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
    try:
        db.execute(
            "INSERT INTO observations (series_id, obs_date, value, dims, revised_at, first_seen_at) "
            "VALUES (%s,'2026-08-31',50,'{}','2026-09-10','2026-09-10')",
            (sid,),
        )
        # a later run re-ingests the same value (the framework sets revised_at=now())
        db.execute(
            "INSERT INTO observations (series_id, obs_date, value, dims, revised_at) "
            "VALUES (%s,'2026-08-31',50,'{}',now()) "
            "ON CONFLICT (series_id, obs_date, dims) DO UPDATE SET value=EXCLUDED.value, revised_at=now()",
            (sid,),
        )
        row = db.query_one("SELECT revised_at::date AS r, first_seen_at::date AS f FROM observations WHERE series_id=%s", (sid,))
        assert str(row["r"]) == "2026-09-10" and str(row["f"]) == "2026-09-10"
        assert not db.query("SELECT 1 FROM observation_revisions WHERE series_id=%s", (sid,))
        # known on 2026-09-20 -> present; not yet held on 2026-09-05 -> absent
        r = client.get(f"/v1/series/{sid}", params={"vintage": "2026-09-20"}, headers=_auth(api_key))
        assert [d["value"] for d in r.json()["data"]] == [50.0]
        r = client.get(f"/v1/series/{sid}", params={"vintage": "2026-09-05"}, headers=_auth(api_key))
        assert r.json()["data"] == []
    finally:
        db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM series WHERE id=%s", (sid,))


def test_revisions_endpoint_lists_first_print_and_chain(client, api_key):
    """/revisions: first print, each superseded figure with its end date + source, today's value."""
    from app import db

    sid = "test.vintage.revisions"
    db.execute(
        "INSERT INTO series (id, module, name, frequency, source, tier, is_active) "
        "VALUES (%s,'prices','Revisions Test','monthly','TEST','basic',true) ON CONFLICT (id) DO NOTHING",
        (sid,),
    )
    db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
    db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
    try:
        db.execute("INSERT INTO observations (series_id, obs_date, value, dims, first_seen_at) VALUES "
                   "(%s,'2026-01-31',100,'{}','2026-02-28'), (%s,'2026-02-28',7,'{}','2026-03-31')", (sid, sid))
        db.execute("UPDATE observations SET value=105 WHERE series_id=%s AND obs_date='2026-01-31'", (sid,))
        db.execute("UPDATE observations SET value=110 WHERE series_id=%s AND obs_date='2026-01-31'", (sid,))
        db.execute("UPDATE observation_revisions SET superseded_at = CASE WHEN value=100 THEN "
                   "'2026-03-31'::timestamptz ELSE '2026-04-30'::timestamptz END, "
                   "source = CASE WHEN value=100 THEN 'sbp_msb' END WHERE series_id=%s", (sid,))
        r = client.get(f"/v1/series/{sid}/revisions", params={"sort": "asc"}, headers=_auth(api_key))
        assert r.status_code == 200, r.text
        body = r.json()
        jan, feb = body["data"]
        assert jan["value"] == 110.0 and jan["first_print"] == 100.0 and jan["first_seen"] == "2026-02-28"
        assert [(v["value"], v["until"]) for v in jan["revisions"]] == [(100.0, "2026-03-31"), (105.0, "2026-04-30")]
        assert jan["revisions"][0]["source"] == "SBP Monthly Statistical Bulletin"
        assert feb["first_print"] == 7.0 and feb["revisions"] == []
        assert body["pagination"]["revised_points"] == 1
        only = client.get(f"/v1/series/{sid}/revisions", params={"changed_only": "true"}, headers=_auth(api_key))
        assert [d["date"] for d in only.json()["data"]] == ["2026-01-31"]
    finally:
        db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM series WHERE id=%s", (sid,))


def test_vintage_requires_pro_plan(client):
    """Vintages are a Pro+ feature; a free key gets 403."""
    from app import db
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    u = db.query_one(
        "INSERT INTO users (email, plan, subscription_status) VALUES (%s,'free','active') RETURNING id",
        (f"pytest-free-{os.getpid()}@pakdata.local",),
    )
    db.execute("INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'free')", (key_hash, u["id"]))
    try:
        r = client.get("/v1/series/inflation.cpi.national",
                       params={"vintage": "2026-01-01"}, headers=_auth(plaintext))
        assert r.status_code == 403
        r = client.get("/v1/series/inflation.cpi.national/revisions", headers=_auth(plaintext))
        assert r.status_code == 403
    finally:
        db.execute("DELETE FROM api_keys WHERE key_hash=%s", (key_hash,))
        db.execute("DELETE FROM users WHERE id=%s", (u["id"],))


# ---- dedicated datasets ------------------------------------------------------

def test_funds_list_and_nav_history(client, api_key):
    r = client.get("/v1/funds", params={"limit": 5}, headers=_auth(api_key))
    assert r.status_code == 200
    funds = r.json()["data"]
    assert funds
    fid = funds[0]["fund_id"]
    nav = client.get(f"/v1/funds/{fid}/nav", params={"limit": 5}, headers=_auth(api_key))
    assert nav.status_code == 200
    assert all(p["nav"] is None or p["nav"] > 0 for p in nav.json()["data"])


def test_unknown_fund_is_404(client, api_key):
    assert client.get("/v1/funds/99999999", headers=_auth(api_key)).status_code == 404


def test_funds_screener_one_call(client, api_key):
    """/v1/funds/screener returns every fund with returns/TER in one response, and
    the literal `screener` path is not shadowed by the /{fund_id} int route."""
    r = client.get("/v1/funds/screener", headers=_auth(api_key))
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    rows = body["data"]
    assert rows and body["count"] == len(rows)
    sample = rows[0]
    assert {"fund_id", "name", "amc", "category", "returns", "ter_ytd"} <= set(sample)
    # at least some funds carry a trailing return and a TER (data is present)
    assert any(f["returns"] and f["returns"].get("y1") is not None for f in rows)


def test_securities_normalised_sukuk(client, api_key):
    """Every sukuk must carry the canonical identity, not a legacy code."""
    import re

    r = client.get("/v1/securities", params={"type": "gis_sukuk", "limit": 500},
                   headers=_auth(api_key))
    assert r.status_code == 200
    rows = r.json()["data"]
    assert rows
    pattern = re.compile(r"^GOPIS-(VRR|FRR)-\d{4}-\d{2}-\d{2}$")
    assert all(pattern.match(s["code"]) for s in rows)


def test_securities_bad_type_is_422(client, api_key):
    r = client.get("/v1/securities", params={"type": "nonsense"}, headers=_auth(api_key))
    assert r.status_code == 422


def test_trade_flow_filter(client, api_key):
    r = client.get("/v1/trade", params={"flow": "export", "limit": 5},
                   headers=_auth(api_key))
    assert r.status_code == 200
    rows = r.json()["data"]
    assert all(x["flow"] == "export" for x in rows)


def test_trade_bad_flow_is_422(client, api_key):
    r = client.get("/v1/trade", params={"flow": "sideways"}, headers=_auth(api_key))
    assert r.status_code == 422


def test_trade_hs_partner_lookups_match_country_case_insensitively(client, api_key):
    """partner-products / partner-history / hs resolve the partner to its stored spelling (an exact,
    indexed match) — "china" still finds "China"."""
    from app import db
    from app.services import cache

    def forget():
        for k in ("hs_countries:export:v1", "partners:export:2099::50", "partner_products:export:2099:Zz-Testland:50",
                  "partner_history:export:Zz-Testland:0"):
            try:
                cache.get_redis().delete("cache:v1:trade:" + k)
            except Exception:
                pass

    forget()
    rows = [("export", 2099, "TOTAL", "All countries", None, 100.0), ("export", 2099, "0101", "All countries", 1, 60.0),
            ("export", 2099, "0101", "Zz-Testland", 1, 60.0), ("export", 2099, "0202", "Zz-Testland", 2, 40.0)]
    db.execute("INSERT INTO trade_hs_codes (code, classification, description) VALUES ('0101','HS','LIVE HORSES'), "
               "('0202','HS','FROZEN BEEF') ON CONFLICT (code) DO NOTHING")
    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO trade_hs VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", rows)
    try:
        r = client.get("/v1/trade/partner-products", params={"country": "zz-TESTLAND", "fy": 2099},
                       headers=_auth(api_key))
        assert r.status_code == 200
        assert [x["code"] for x in r.json()["data"]] == ["0101", "0202"]
        r = client.get("/v1/trade/partner-history", params={"country": "zz-testland"}, headers=_auth(api_key))
        assert [x["value_pkr_th"] for x in r.json()["data"] if x["fy"] == 2099] == [100.0]
        r = client.get("/v1/trade/hs", params={"code": "0101", "country": "ZZ-testland"}, headers=_auth(api_key))
        assert [x["country"] for x in r.json()["data"]] == ["Zz-Testland"]
        r = client.get("/v1/trade/partners", params={"fy": 2099}, headers=_auth(api_key))
        assert [(x["country"], x["share_pct"]) for x in r.json()["data"]] == [("Zz-Testland", 100.0)]
    finally:
        db.execute("DELETE FROM trade_hs WHERE fy = 2099")
        forget()


def test_auctions_endpoint(client, api_key):
    r = client.get("/v1/fixed-income/auctions", params={"type": "tbill"},
                   headers=_auth(api_key))
    assert r.status_code == 200
    assert all(a["type"] == "tbill" for a in r.json()["data"])


def test_commodities_accepts_all_item_spellings(client, api_key):
    """Regression: ITEM_PREFIX was 'commodity.' after the ids became 'commodities.*',
    so every documented form 404'd."""
    h = {"X-API-Key": api_key}
    for item in ("wheat_flour", "commodities.wheat_flour", "commodity.wheat_flour"):
        r = client.get(f"/v1/commodities?item={item}&limit=1", headers=h)
        assert r.status_code == 200, (item, r.text)
        assert r.json()["series"] == "commodities.wheat_flour"
    r = client.get("/v1/commodities/by-city?item=wheat_flour&city=lahore&limit=1", headers=h)
    assert r.status_code == 200, r.text


def test_transform_yoy_not_null_inside_small_window(client, api_key):
    """Regression: yoy was computed over the `limit`-row window only, so all but the
    newest rows came back null. The partners must be fetched as a lookback."""
    h = {"X-API-Key": api_key}
    r = client.get("/v1/series/inflation.cpi.national?transform=yoy&limit=14", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert len(data) == 14
    assert all(p["value"] is not None for p in data), data


def test_series_meta_carries_notes_for_returned_points(client, api_key):
    """observation_notes surface as meta.notes — only for points in the returned window."""
    from app import db

    sid = "test.notes.series"
    db.execute(
        "INSERT INTO series (id, module, name, frequency, source, tier, is_active) "
        "VALUES (%s,'external','Notes Test','monthly','TEST','basic',true) ON CONFLICT (id) DO NOTHING", (sid,))
    db.execute("""CREATE TABLE IF NOT EXISTS observation_notes (series_id TEXT NOT NULL, obs_date DATE NOT NULL,
                  dims JSONB NOT NULL DEFAULT '{}', note TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'as_published',
                  evidence TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (series_id, obs_date, dims))""")
    try:
        db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES "
                   "(%s,'2026-01-31',1.2,'{}'), (%s,'2026-02-28',3643.8,'{}'), (%s,'2026-03-31',1.3,'{}')",
                   (sid, sid, sid))
        db.execute("INSERT INTO observation_notes (series_id, obs_date, note) VALUES (%s,'2026-02-28',"
                   "'As published by SBP; about 3,000x the neighbouring months (likely a unit slip at source).')", (sid,))
        r = client.get(f"/v1/series/{sid}", headers=_auth(api_key))
        assert r.status_code == 200, r.text
        notes = r.json()["meta"]["notes"]
        assert [(n["date"], n["kind"]) for n in notes] == [("2026-02-28", "as_published")]
        r = client.get(f"/v1/series/{sid}", params={"to": "2026-01-31"}, headers=_auth(api_key))
        assert r.json()["meta"]["notes"] is None
    finally:
        db.execute("DELETE FROM observation_notes WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM observations WHERE series_id=%s", (sid,))
        db.execute("DELETE FROM series WHERE id=%s", (sid,))


def test_calendar_ics_feed():
    from fastapi.testclient import TestClient

    from app.main import app
    client = TestClient(app)
    r = client.get("/v1/calendar.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.rstrip().endswith("END:VCALENDAR")
    assert body.count("BEGIN:VEVENT") == body.count("END:VEVENT")
    keys = ("BEGIN", "END", "VERSION", "PRODID", "CALSCALE", "METHOD", "X-WR-", "REFRESH", "UID", "DTSTAMP",
            "DTSTART", "DTEND", "SUMMARY", "DESCRIPTION", "URL", "TRANSP")
    assert all(ln.startswith(keys) for ln in body.split("\r\n") if ln)  # no raw newline inside a value
    uids = lambda text: [ln for ln in text.splitlines() if ln.startswith("UID:")]  # noqa: E731
    assert uids(body) == uids(client.get("/v1/calendar.ics").text)  # stable, so subscribers update in place
