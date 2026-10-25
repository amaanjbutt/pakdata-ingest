"""Credit-metered free tier (2026-08-20; monthly allowance since 2026-10-04).

Free accounts get a monthly request allowance consumed per API request, refilled
on the first request of a new calendar month; paid plans are not credit-metered. Skipped automatically when no
database is reachable (like test_api_integration), so `pytest` still runs offline.

    docker compose exec api python -m pytest tests/test_credits.py
"""
from __future__ import annotations

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


def _make_user(plan: str, credits: int | None):
    """Create a user (+ one key) on `plan`, optionally seeding credits. Returns
    (plaintext_key, cleanup_fn)."""
    from app import db
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    row = db.query_one(
        "INSERT INTO users (email, plan, subscription_status) "
        "VALUES (%s,%s,'active') RETURNING id",
        (f"pytest-credits-{plan}-{uuid.uuid4().hex[:12]}@pakdata.local", plan),
    )
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


def test_free_tier_exhausts_credits_then_402(client):
    """A free key with 2 credits serves 2 requests, then 402 credits_exhausted."""
    key, cleanup = _make_user("free", credits=2)
    try:
        h = {"X-API-Key": key}
        assert client.get("/v1/usage", headers=h).status_code == 200
        assert client.get("/v1/usage", headers=h).status_code == 200
        r = client.get("/v1/usage", headers=h)
        assert r.status_code == 402
        assert r.json()["error"]["code"] == "credits_exhausted"
    finally:
        cleanup()


def test_free_usage_surfaces_remaining_credits(client):
    key, cleanup = _make_user("free", credits=5)
    try:
        body = client.get("/v1/usage", headers={"X-API-Key": key}).json()
        # limits.credits is the plan's one-time allotment; the per-user balance is
        # usage.credits_remaining. One credit consumed by this very request -> 4 left.
        from app.security import FREE_CREDITS
        assert body["limits"]["credits"] == FREE_CREDITS
        assert body["usage"]["credits_remaining"] == 4
    finally:
        cleanup()


def test_paid_plan_is_not_credit_metered(client):
    """A pro key with 0 credits still serves — paid plans ignore credits."""
    key, cleanup = _make_user("pro", credits=0)
    try:
        h = {"X-API-Key": key}
        for _ in range(3):
            assert client.get("/v1/usage", headers=h).status_code == 200
        assert client.get("/v1/usage", headers=h).json()["usage"]["credits_remaining"] is None
    finally:
        cleanup()


def test_plans_endpoint_advertises_free_credits(client):
    plans = {p["plan"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert plans["free"]["free_credits"] == 500
    assert plans["pro"]["free_credits"] is None


def test_free_allowance_resets_in_a_new_month(client):
    """A free key that spent last month's allowance is refilled on its first
    request this month (the refill happens inside the atomic decrement)."""
    from app import db
    from app.security import FREE_CREDITS

    key, cleanup = _make_user("free", credits=0)
    try:
        from app.security import hash_key

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


def test_plans_endpoint_sells_value_not_volume(client):
    plans = {p["plan"]: p for p in client.get("/v1/plans").json()["plans"]}
    assert plans["free"]["free_credits_period"] == "month"
    assert plans["business"]["usage_rights"] == "display"
    assert plans["pro"]["usage_rights"] == "internal"
    assert plans["pro"]["vintages"] is True and plans["developer"]["vintages"] is False
    assert plans["developer"]["fair_use"] is True and plans["free"]["fair_use"] is False
    assert plans["business"]["webhooks"] is None
