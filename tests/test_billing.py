"""Billing: webhook signature, plan mapping, event parsing (offline), plus
DB-backed idempotency / provisioning / claim-once (auto-skip without a database).
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.billing import lemonsqueezy as ls
from app.billing import service


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---- signature verification (security boundary) -----------------------------

def test_valid_signature_passes():
    body = b'{"hello":"world"}'
    assert ls.verify_signature(body, _sign(body, "s3cret"), "s3cret")


def test_tampered_body_fails():
    good = _sign(b'{"amount":20}', "s3cret")
    assert not ls.verify_signature(b'{"amount":2000}', good, "s3cret")


def test_missing_signature_or_secret_fails_closed():
    body = b"{}"
    assert not ls.verify_signature(body, None, "s3cret")
    assert not ls.verify_signature(body, _sign(body, "s3cret"), "")


# ---- variant -> plan mapping ------------------------------------------------

def test_plan_for_variant(monkeypatch):
    monkeypatch.setattr(ls.settings, "lemonsqueezy_variant_developer", "111")
    monkeypatch.setattr(ls.settings, "lemonsqueezy_variant_pro", "222")
    monkeypatch.setattr(ls.settings, "lemonsqueezy_variant_business", "333")
    assert ls.plan_for_variant("111") == "developer"
    assert ls.plan_for_variant(222) == "pro"        # numeric id tolerated
    assert ls.plan_for_variant("333") == "business"
    assert ls.plan_for_variant("999") is None
    assert ls.plan_for_variant(None) is None


def test_ls_status_mapping():
    assert ls.LS_STATUS["active"] == "active"
    assert ls.LS_STATUS["cancelled"] == "active"    # usable until it expires
    assert ls.LS_STATUS["past_due"] == "past_due"
    assert ls.LS_STATUS["expired"] == "canceled"


# ---- webhook payload parsing ------------------------------------------------

_PAYLOAD = {
    "meta": {"event_name": "subscription_created",
             "custom_data": {"signup_token": "tok_abc"}},
    "data": {"type": "subscriptions", "id": "sub_1",
             "attributes": {"customer_id": 42, "variant_id": 222,
                            "user_email": "Buyer@Example.com ", "status": "active",
                            "renews_at": "2026-09-01T00:00:00Z", "ends_at": None}},
}


def test_parse_subscription():
    s = service.parse_subscription(_PAYLOAD)
    assert s["event_name"] == "subscription_created"
    assert s["signup_token"] == "tok_abc"
    assert s["subscription_id"] == "sub_1"
    assert s["variant_id"] == 222
    assert s["email"] == "buyer@example.com"        # normalized
    assert s["ls_status"] == "active"
    assert s["period_end"] == "2026-09-01T00:00:00Z"


# ---- DB-backed flow (skips cleanly without a database) ----------------------

def _db_available() -> bool:
    try:
        from app import db
        db.query_one("SELECT 1")
        return True
    except Exception:
        return False


db_only = pytest.mark.skipif(not _db_available(), reason="needs a database")


@db_only
def test_idempotent_replay_does_not_double_provision(monkeypatch):
    from app import db
    monkeypatch.setattr(ls.settings, "lemonsqueezy_variant_pro", "222")
    email = "replaytest@example.com"
    db.execute("DELETE FROM users WHERE email=%s", (email,))
    payload = json.loads(json.dumps(_PAYLOAD))
    payload["data"]["attributes"]["user_email"] = email
    sig = "sig_replay_test"

    first = service.already_processed(sig, "subscription_created")
    assert first is False
    service.handle_event(payload)
    # replay: same signature -> deduped, no second provisioning
    assert service.already_processed(sig, "subscription_created") is True

    users = db.query("SELECT plan, subscription_status FROM users WHERE email=%s", (email,))
    assert len(users) == 1 and users[0]["plan"] == "pro"
    claims = db.query(
        "SELECT count(*) c FROM key_claims kc JOIN users u ON u.id=kc.user_id WHERE u.email=%s",
        (email,))
    assert claims[0]["c"] == 1
    db.execute("DELETE FROM billing_events WHERE idempotency_key=%s", (sig,))
    db.execute("DELETE FROM key_claims WHERE user_id=(SELECT id FROM users WHERE email=%s)", (email,))
    db.execute("DELETE FROM users WHERE email=%s", (email,))


@db_only
def test_claim_key_is_one_time(monkeypatch):
    from app import db
    monkeypatch.setattr(ls.settings, "lemonsqueezy_variant_pro", "222")
    email = "claimtest@example.com"
    token = "tok_claim_test"
    db.execute("DELETE FROM users WHERE email=%s", (email,))
    payload = json.loads(json.dumps(_PAYLOAD))
    payload["data"]["attributes"]["user_email"] = email
    payload["meta"]["custom_data"]["signup_token"] = token
    service.handle_event(payload)

    key1 = service.claim_key(token)
    assert key1 and key1.startswith("pk_live_")
    assert service.claim_key(token) is None          # second claim rejected

    n = db.query_one(
        "SELECT count(*) c FROM api_keys WHERE user_id=(SELECT id FROM users WHERE email=%s)",
        (email,))
    assert n["c"] == 1
    db.execute("DELETE FROM api_keys WHERE user_id=(SELECT id FROM users WHERE email=%s)", (email,))
    db.execute("DELETE FROM key_claims WHERE user_id=(SELECT id FROM users WHERE email=%s)", (email,))
    db.execute("DELETE FROM users WHERE email=%s", (email,))
