"""Free-plan metering (2026-11-20): 20 API calls a day, reset at 00:00 UTC.

Free keys that used more than 20 calls a day before the switch are grandfathered for 60 days at the
old allowance — 500 calls a month (users.legacy_free_until), refilled on the first request of a new
calendar month. Paid plans are never credit-metered. Skipped automatically when no database is
reachable (like test_api_integration), so `pytest` still runs offline.

    docker compose exec api python -m pytest tests/test_credits.py
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

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


def _make_user(plan: str, credits: int | None, *, legacy: bool = False):
    """Create a user (+ one key) on `plan`, optionally grandfathered at the old free allowance and
    seeded with credits. Returns (plaintext_key, cleanup_fn)."""
    from app import db
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    row = db.query_one(
        "INSERT INTO users (email, plan, subscription_status) "
        "VALUES (%s,%s,'active') RETURNING id",
        (f"pytest-credits-{plan}-{uuid.uuid4().hex[:12]}@pakdata.local", plan),
    )
    if legacy:
        db.execute("UPDATE users SET legacy_free_until = %s WHERE id = %s",
                   (date.today() + timedelta(days=60), row["id"]))
    if credits is not None:
        db.execute("UPDATE users SET credits_remaining=%s WHERE id=%s", (credits, row["id"]))
    db.execute(
        "INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'pytest')",
        (key_hash, row["id"]),
    )

    def cleanup():
        db.execute("DELETE FROM api_keys WHERE key_hash=%s", (key_hash,))
        db.execute("DELETE FROM users WHERE id=%s", (row["id"],))

    return plaintext, cleanup


def _user_id(key: str) -> str:
    from app import db
    from app.security import hash_key

    return str(db.query_one("SELECT user_id FROM api_keys WHERE key_hash=%s", (hash_key(key),))["user_id"])


def test_free_plan_stops_after_20_calls_a_day(client):
    from app.security import _utc_day, get_redis

    key, cleanup = _make_user("free", credits=None)
    try:
        h = {"X-API-Key": key}
        r = client.get("/v1/usage", headers=h)
        assert r.status_code == 200
        assert r.headers["X-RateLimit-Day-Limit"] == "20"
        assert r.headers["X-Page-Max"] == "1000"
        body = r.json()
        assert body["limits"]["calls_per_day"] == 20 and body["usage"]["credits_remaining"] is None
        assert body["limits"]["downloads_per_day"] == 5
        # spend the rest of the day without tripping the 10/minute limit
        get_redis().set(f"rl:day:{_user_id(key)}:{_utc_day()}", 20)
        r = client.get("/v1/usage", headers=h)
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "daily_limit"
        assert int(r.headers["Retry-After"]) > 0
    finally:
        cleanup()


def test_grandfathered_free_key_exhausts_monthly_credits_then_402(client):
    key, cleanup = _make_user("free", credits=2, legacy=True)
    try:
        h = {"X-API-Key": key}
        assert client.get("/v1/usage", headers=h).status_code == 200
        assert client.get("/v1/usage", headers=h).status_code == 200
        r = client.get("/v1/usage", headers=h)
        assert r.status_code == 402
        assert r.json()["error"]["code"] == "credits_exhausted"
    finally:
        cleanup()


def test_grandfathered_usage_surfaces_remaining_credits(client):
    key, cleanup = _make_user("free", credits=5, legacy=True)
    try:
        body = client.get("/v1/usage", headers={"X-API-Key": key}).json()
        from app.security import FREE_CREDITS
        assert body["limits"]["credits"] == FREE_CREDITS
        assert body["usage"]["credits_remaining"] == 4  # this very request spent one
        assert body["grandfathered"]["free_allowance_until"]
    finally:
        cleanup()


def test_paid_plan_is_not_credit_metered(client):
    """A pro key with 0 credits still serves — paid plans ignore credits."""
    key, cleanup = _make_user("pro", credits=0)
    try:
        h = {"X-API-Key": key}
        for _ in range(3):
            assert client.get("/v1/usage", headers=h).status_code == 200
        body = client.get("/v1/usage", headers=h).json()
        assert body["usage"]["credits_remaining"] is None
        assert body["limits"]["calls_per_day"] == 20_000
    finally:
        cleanup()


def test_grandfathered_allowance_resets_in_a_new_month(client):
    from app import db
    from app.security import FREE_CREDITS, hash_key

    key, cleanup = _make_user("free", credits=0, legacy=True)
    try:
        db.execute(
            "UPDATE users SET credits_period = (date_trunc('month', now()) - interval '1 month')::date "
            "WHERE id = (SELECT user_id FROM api_keys WHERE key_hash = %s)",
            (hash_key(key),),
        )
        body = client.get("/v1/usage", headers={"X-API-Key": key}).json()
        assert body["success"] is True
        assert body["usage"]["credits_remaining"] == FREE_CREDITS - 1
        assert body["usage"]["credits_reset"]  # first day of next month
    finally:
        cleanup()


def test_plans_endpoint_is_truthful(client):
    body = client.get("/v1/plans").json()
    plans = {p["plan"]: p for p in body["plans"]}
    assert list(plans) == ["free", "pass", "developer", "pro", "business"]
    free = plans["free"]
    assert free["calls_per_day"] == 20 == free["requests_per_day"]
    assert free["downloads_per_day"] == 5 and free["max_points_per_call"] == 1000
    assert free["history"] == "last 5 years"
    p = plans["pass"]
    assert p["kind"] == "one_time" and p["price_usd_once"] == 9 and p["pass_days"] == 30
    assert p["calls_per_day"] == 500 and p["downloads_per_day"] is None and p["whole_dataset_files"]
    assert plans["developer"]["calls_per_day"] == 2_000 and not plans["developer"]["whole_dataset_files"]
    assert plans["pro"]["calls_per_day"] == 20_000 and plans["pro"]["vintages_api"] is True
    assert plans["business"]["calls_per_day"] == 200_000 and plans["business"]["sla"] is True
    assert plans["developer"]["price_usd_year"] == 190  # 2 months free
    assert plans["business"]["webhooks"] is None and plans["pro"]["webhooks"] == 50
    for gone in ("free_credits", "usage_rights", "fair_use"):
        assert gone not in free
    assert "internal" not in plans
    assert body["day_resets"] == "00:00 UTC"
