"""Per-request API log: middleware → Redis stream → api_requests → rollups → archive."""
from __future__ import annotations

import gzip
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services import reqlog
from tests.test_api_integration import _db_available, api_key, client  # noqa: F401 — fixtures


def test_ua_class():
    assert reqlog.ua_class("Mozilla/5.0 (compatible; Googlebot/2.1)") == "bot"
    assert reqlog.ua_class("Mozilla/5.0 (Windows NT 10.0) Chrome/146") == "browser"
    assert reqlog.ua_class("curl/8.5.0") == "curl"
    assert reqlog.ua_class("python-httpx/0.28.1") == "script"
    assert reqlog.ua_class("Mozilla/5.0 (compatible; Google-Apps-Script)") == "sheets"
    assert reqlog.ua_class(None) == "none"
    assert reqlog.ua_class("Mozilla/5.0", from_site=True) == "site"


def test_ip_hash_is_keyed_and_stable():
    h = reqlog.ip_hash("203.0.113.9")
    assert h == reqlog.ip_hash("203.0.113.9") and len(h) == 16
    assert "203" not in h and h != reqlog.ip_hash("203.0.113.10")
    assert reqlog.ip_hash(None) == ""


pytestmark_db = pytest.mark.skipif(not _db_available(), reason="no database")


def _last_entry():
    from app.security import get_redis
    entries = get_redis().xrevrange(reqlog.STREAM, "+", "-", count=1)
    return entries[0][1] if entries else None


@pytestmark_db
def test_middleware_logs_public_and_keyed_calls(client, api_key):  # noqa: F811
    client.get("/v1/plans", headers={"user-agent": "curl/8.5.0", "x-forwarded-for": "198.51.100.7, 10.0.0.1"})
    e = _last_entry()
    assert e["route"] == "/v1/plans" and e["auth"] == "public" and e["status"] == "200"
    assert e["ua"] == "curl" and e["ip_hash"] == reqlog.ip_hash("198.51.100.7")

    client.get("/v1/usage?api_key=" + api_key, headers={"x-pakdata-page": "/dashboard"})
    e = _last_entry()
    assert e["route"] == "/v1/usage" and e["auth"] == "key" and e["user_id"]
    assert e["key_id"] and e["page"] == "/dashboard" and e["ua"] == "site"
    assert "api_key" not in e["subject"]  # query strings are never logged

    client.get("/v1/series/does.not.exist?api_key=" + api_key)
    e = _last_entry()
    assert e["route"] == "/v1/series/{series_id}" and e["subject"] == "does.not.exist"
    assert e["status"] == "404"


@pytestmark_db
def test_flush_rollup_and_usage_endpoints(client, api_key):  # noqa: F811
    from app import db
    from ingestion import request_log

    client.get("/v1/usage?api_key=" + api_key)
    assert request_log.flush() >= 1
    from app.security import get_redis
    assert get_redis().xlen(reqlog.STREAM) == 0
    uid = db.query_one(
        "SELECT user_id FROM api_requests WHERE auth='key' ORDER BY ts DESC LIMIT 1")["user_id"]
    today = datetime.now(timezone.utc).date()
    out = request_log.rollup(today)
    assert out["endpoint_rows"] >= 1
    assert db.query_one("SELECT requests FROM usage_daily WHERE user_id=%s AND day=%s", (uid, today))
    assert db.query_one("SELECT 1 AS x FROM usage_daily_dim WHERE day=%s AND dim='ua'", (today,))
    r = client.get("/v1/usage/endpoints?api_key=" + api_key).json()
    assert r["success"] and any(row["route"] == "/v1/usage" for row in r["data"])


class _FakeStore:
    def __init__(self, ok=True):
        self.ok, self.put_calls = ok, []

    def put(self, d, name, data):
        self.put_calls.append((d, name, gzip.decompress(data).decode()))
        return self.ok


@pytestmark_db
def test_archive_exports_then_drops_old_days_only():
    from app import db
    from ingestion import request_log

    old = date(2001, 1, 1)
    request_log.ensure_partition(old)
    db.execute("INSERT INTO api_requests (ts, auth, route, status, ms) VALUES (%s, 'public', '/v1/plans', 200, 3.0)",
               (datetime(2001, 1, 1, 12, tzinfo=timezone.utc),))
    try:
        failing = _FakeStore(ok=False)
        assert request_log.archive(store=failing, today=old + timedelta(days=45)) == []
        assert db.query_one("SELECT to_regclass(%s) AS t", (request_log.partition_name(old),))["t"]

        store = _FakeStore()
        done = request_log.archive(store=store, today=old + timedelta(days=45))
        assert done == ["api_requests_2001-01-01.csv.gz"]
        assert "/v1/plans" in store.put_calls[0][2] and store.put_calls[0][2].startswith("ts,")
        assert db.query_one("SELECT to_regclass(%s) AS t", (request_log.partition_name(old),))["t"] is None
        assert db.query_one("SELECT 1 AS x FROM usage_daily_endpoint WHERE day=%s", (old,))
    finally:
        db.execute(f"DROP TABLE IF EXISTS {request_log.partition_name(old)}")
        db.execute("DELETE FROM usage_daily_endpoint WHERE day=%s", (old,))
        db.execute("DELETE FROM usage_daily_dim WHERE day=%s", (old,))
