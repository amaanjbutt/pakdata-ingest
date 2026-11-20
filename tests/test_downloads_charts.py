"""Site downloads, full-history charts, whole-dataset files and CSV on every endpoint (plans for developers and researchers, 2026-11-20).

The chart-shaping rules run without a database; the rest is skipped when none is reachable.
"""
from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, timedelta

import pytest

from app.services import datasets

# ---- chart shaping (pure) ------------------------------------------------------------------


def _daily(start: date, end: date, v0: float = 100.0) -> list[dict]:
    out, d, v = [], start, v0
    while d <= end:
        if d.weekday() < 5:
            out.append({"date": d, "value": v})
            v += 0.01
        d += timedelta(days=1)
    return out


def test_daily_series_recent_weekly_older_monthly_average():
    rows = _daily(date(2016, 1, 1), date(2026, 10, 30))
    out = datasets.chart_points(rows, "daily")
    pts = out["points"]
    cutoff = out["recent_from"]
    assert cutoff == date(2021, 10, 30)
    recent = [p for p in pts if p["date"] >= cutoff]
    older = [p for p in pts if p["date"] < cutoff]
    # one published value per week in the last 5 years, nothing averaged there
    assert 255 <= len(recent) <= 265 and all("avg" not in p for p in recent)
    assert recent[-1]["value"] == rows[-1]["value"]
    # older history only as monthly averages, never the published daily values
    assert out["older"] == "month" and all(p["avg"] == "month" and p["date"].day == 1 for p in older)
    published = {r["value"] for r in rows if r["date"] < cutoff}
    assert not any(p["value"] in published for p in older[1:-1])
    assert len(pts) <= datasets.CHART_BUDGET


def test_long_daily_series_falls_back_to_annual_averages():
    rows = _daily(date(1990, 1, 1), date(2026, 10, 30))
    out = datasets.chart_points(rows, "daily")
    assert out["older"] == "year"
    assert len(out["points"]) <= datasets.CHART_BUDGET


def test_monthly_series_older_years_are_annual_averages():
    rows = [{"date": date(1990 + m // 12, m % 12 + 1, 1), "value": float(m)} for m in range(36 * 12)]
    out = datasets.chart_points(rows, "monthly")
    recent = [p for p in out["points"] if "avg" not in p]
    older = [p for p in out["points"] if "avg" in p]
    assert out["older"] == "year" and all(p["avg"] == "year" for p in older)
    assert len(recent) == 61  # 5 years of months, at published precision (inclusive cutoff)
    assert older[0] == {"date": date(1990, 1, 1), "value": 5.5, "avg": "year"}


def test_annual_series_stay_exact():
    rows = [{"date": date(1960 + i, 6, 30), "value": float(i)} for i in range(66)]
    out = datasets.chart_points(rows, "annual")
    assert out["older"] is None
    assert [p["value"] for p in out["points"]] == [r["value"] for r in rows]


def test_csv_has_citation_and_dims_columns():
    ds = datasets.Dataset(kind="series", subject="x.y", name="KIBOR 3M", unit="percent", frequency="daily",
                          source="SBP", page="https://pakdatahub.com/series/x.y",
                          rows=[{"date": date(2026, 1, 2), "value": -1.5, "dims": {"side": "offer"}},
                                {"date": date(2026, 1, 2), "value": 10.0, "dims": {"side": "bid"}}],
                          dims_keys=["side"])
    text = datasets.to_csv(ds).decode()
    first, rest = text.split("\n", 1)
    assert first.startswith("# KIBOR 3M. Source: SBP, via PakDataHub") and "credit PakDataHub" in first
    rows = list(csv.reader(io.StringIO(rest)))
    assert rows[0] == ["date", "side", "value"]
    assert rows[1] == ["2026-01-02", "offer", "-1.5"]  # a negative number stays a number


def test_tabular_flattens_nested_rows():
    from app.services.tabular import rows_to_csv

    out = rows_to_csv([{"fund_id": 1, "latest_nav": {"date": "2026-01-01", "nav": 10.5}, "tags": ["a"]},
                       {"fund_id": 2, "latest_nav": None, "name": "=HYPERLINK()"}])
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["fund_id", "latest_nav.date", "latest_nav.nav", "tags", "latest_nav", "name"]
    assert rows[2][-1] == "'=HYPERLINK()"


# ---- API (needs Postgres + Redis) ------------------------------------------------------------

fastapi_testclient = pytest.importorskip("fastapi.testclient")


def _db_available() -> bool:
    try:
        from app import db

        db.query_one("SELECT 1 AS ok")
        from app.security import get_redis

        get_redis().ping()
        return True
    except Exception:
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="no database reachable")
SID = "pytest_dl.cpi"


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return fastapi_testclient.TestClient(app)


@pytest.fixture(scope="module")
def series():
    from app import db

    if not _db_available():
        pytest.skip("no database reachable")
    db.execute("INSERT INTO series (id, module, name, unit, frequency, source, tier, is_active) VALUES "
               "(%s,'prices','Pytest CPI','index','monthly','PBS','basic',true) ON CONFLICT (id) DO NOTHING", (SID,))
    db.execute("DELETE FROM observations WHERE series_id=%s", (SID,))
    for m in range(30 * 12):  # 1996-01 .. 2025-12
        db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES (%s,%s,%s,'{}')",
                   (SID, date(1996 + m // 12, m % 12 + 1, 1), 50 + m))
    yield SID
    db.execute("DELETE FROM observations WHERE series_id=%s", (SID,))
    db.execute("DELETE FROM observation_revisions WHERE series_id=%s", (SID,))
    db.execute("DELETE FROM series WHERE id=%s", (SID,))


def _account(plan: str = "free", **cols):
    """A user with a session token and an API key. Returns (session, key, user_id, cleanup)."""
    from app import db
    from app.security import create_session, generate_key, get_redis

    row = db.query_one("INSERT INTO users (email, plan, subscription_status) VALUES (%s,%s,'active') RETURNING id",
                       (f"pytest-dl-{uuid.uuid4().hex[:12]}@pakdata.local", plan))
    uid = str(row["id"])
    for k, v in cols.items():
        db.execute(f"UPDATE users SET {k} = %s WHERE id = %s", (v, uid))
    token = create_session(uid)
    key, key_hash = generate_key()
    db.execute("INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'pytest')", (key_hash, uid))

    def cleanup():
        r = get_redis()
        for k in r.scan_iter(f"*{uid}*"):
            r.delete(k)
        for t in ("downloads", "sessions", "api_keys"):
            db.execute(f"DELETE FROM {t} WHERE user_id=%s", (uid,))
        db.execute("DELETE FROM users WHERE id=%s", (uid,))

    return token, key, uid, cleanup


@needs_db
def test_free_account_downloads_five_a_day_with_a_signed_link(client, series):
    token, key, uid, cleanup = _account()
    try:
        h = {"X-Session-Token": token}
        # an API key can't use the website's download button
        assert client.post(f"/v1/downloads/{series}", headers={"X-API-Key": key}).status_code == 401
        links = []
        for i in range(5):
            r = client.post(f"/v1/downloads/{series}", params={"format": "csv" if i else "xlsx"}, headers=h)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["downloads"] == {"used": i + 1, "limit": 5, "remaining": 4 - i}
            links.append(body["url"])
        r = client.post(f"/v1/downloads/{series}", headers=h)
        assert r.status_code == 429 and r.json()["error"]["code"] == "download_limit"
        # the file: full history (beyond the free API's 5 years), attachment, citation line
        f = client.get(links[1].split("testserver", 1)[-1])
        assert f.status_code == 200
        assert f.headers["content-type"].startswith("text/csv")
        assert f.headers["content-disposition"].startswith('attachment; filename="pakdatahub_pytest_dl.cpi_')
        lines = f.text.splitlines()
        assert lines[0].startswith("# Pytest CPI. Source: PBS, via PakDataHub")
        assert lines[1] == "date,value" and lines[2].startswith("1996-01-01,")
        assert len(lines) == 2 + 360
        x = client.get(links[0].split("testserver", 1)[-1])
        assert x.status_code == 200 and x.content[:2] == b"PK"  # an .xlsx is a zip
        # a tampered link is refused
        assert client.get(links[1].split("testserver", 1)[-1][:-3] + "abc").status_code == 404
        listed = client.get("/v1/downloads", headers=h).json()
        assert listed["downloads"]["remaining"] == 0 and len(listed["recent"]) == 5
    finally:
        cleanup()


@needs_db
def test_vintage_download_needs_pro(client, series):
    for plan, want in (("free", 403), ("developer", 403), ("pro", 200)):
        token, _, _, cleanup = _account(plan)
        try:
            r = client.post(f"/v1/downloads/{series}", params={"vintage": "2020-01-01"},
                            headers={"X-Session-Token": token})
            assert r.status_code == want, (plan, r.text)
        finally:
            cleanup()


@needs_db
def test_paid_plans_download_without_a_daily_limit(client, series):
    token, _, _, cleanup = _account("developer")
    try:
        for _ in range(7):
            r = client.post(f"/v1/downloads/{series}", headers={"X-Session-Token": token})
            assert r.status_code == 200
        assert r.json()["downloads"]["limit"] is None
    finally:
        cleanup()


@needs_db
def test_full_history_chart_for_a_signed_in_account(client, series):
    from app.security import _utc_day, get_redis

    token, key, uid, cleanup = _account()
    try:
        assert client.get(f"/v1/chart/series/{series}", headers={"X-API-Key": key}).status_code == 401
        r = client.get(f"/v1/chart/series/{series}", headers={"X-Session-Token": token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["first_date"] == "1996-01-01" and body["count"] == 360
        assert body["older"] == "year"
        assert body["points"][0] == {"date": "1996-01-01", "value": 55.5, "avg": "year"}
        assert len(body["table"]) == 24 and body["table"][0]["date"] == "2025-12-01"
        assert body["downloads"]["limit"] == 5
        # 50 series a day on the free plan; the same series again is free
        r = get_redis()
        k = f"ch:day:{uid}:{_utc_day()}"
        r.sadd(k, *[f"other:{i}" for i in range(49)])
        assert client.get(f"/v1/chart/series/{series}", headers={"X-Session-Token": token}).status_code == 200
        r.sadd(k, "other:49")
        again = client.get(f"/v1/chart/series/{series}", headers={"X-Session-Token": token})
        assert again.status_code == 200
        new = client.get(f"/v1/chart/series/{series}", params={"transform": "yoy"}, headers={"X-Session-Token": token})
        assert new.status_code == 429 and new.json()["error"]["code"] == "chart_limit"
    finally:
        cleanup()


@needs_db
def test_free_api_is_last_five_years_and_1000_points(client, series):
    _, key, _, cleanup = _account()
    try:
        r = client.get(f"/v1/series/{series}", params={"limit": 5000, "sort": "asc"}, headers={"X-API-Key": key})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data and data[0]["date"] >= (date.today().replace(year=date.today().year - 5)).isoformat()
        assert r.headers["X-Page-Max"] == "1000"
    finally:
        cleanup()


@needs_db
def test_plan_change_ends_grandfathering(monkeypatch):
    from app import db
    from app.billing import service
    from app.config import settings

    monkeypatch.setattr(settings, "lemonsqueezy_variant_pro", "501")
    monkeypatch.setattr(settings, "lemonsqueezy_variant_business", "502")
    email = f"pytest-gf-{uuid.uuid4().hex[:8]}@pakdata.local"
    db.execute("INSERT INTO users (email, plan, subscription_status, limits_version) VALUES (%s,'pro','active',1)",
               (email,))
    try:
        sub = {"meta": {"event_name": "subscription_updated"},
               "data": {"id": "s1", "attributes": {"variant_id": 501, "user_email": email, "status": "active"}}}
        service.handle_event(sub)  # same plan: still grandfathered
        assert db.query_one("SELECT limits_version FROM users WHERE email=%s", (email,))["limits_version"] == 1
        sub["data"]["attributes"]["variant_id"] = 502
        service.handle_event(sub)  # Pro -> Business: today's limits
        assert db.query_one("SELECT limits_version FROM users WHERE email=%s", (email,))["limits_version"] == 2
    finally:
        db.execute("DELETE FROM users WHERE email=%s", (email,))


@needs_db
def test_bulk_files_need_pro(client):
    from app import db

    db.execute("INSERT INTO bulk_files (dataset, format, object_key, bytes, series_count, rows) VALUES "
               "('pytestds','csv.gz','bulk/pytestds/pakdatahub_pytestds.csv.gz',10,1,1) "
               "ON CONFLICT (dataset, format) DO NOTHING")
    token, key, _, cleanup = _account()
    try:
        body = client.get("/v1/bulk", headers={"X-Session-Token": token}).json()
        assert body["included"] is False and "Pro and Business" in body["upgrade"]
        ds = next(d for d in body["data"] if d["dataset"] == "pytestds")
        assert ds["files"][0]["url"] is None
        assert client.get("/v1/bulk/pytestds", headers={"X-Session-Token": token}).status_code == 403
    finally:
        cleanup()
        db.execute("DELETE FROM bulk_files WHERE dataset='pytestds'")


@needs_db
def test_csv_on_every_list_endpoint(client):
    _, key, _, cleanup = _account("pro")
    try:
        r = client.get("/v1/funds", params={"format": "csv", "limit": 5}, headers={"X-API-Key": key})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
        header = next(csv.reader(io.StringIO(r.text)))
        assert header[:2] == ["fund_id", "name"]
        bad = client.get("/v1/plans", params={"format": "csv"})
        assert bad.status_code == 422
        js = client.get("/v1/funds", params={"limit": 1}, headers={"X-API-Key": key})
        assert js.headers["content-type"].startswith("application/json") and json.loads(js.text)["success"]
    finally:
        cleanup()
