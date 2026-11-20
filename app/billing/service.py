"""Billing provisioning: turn verified webhook events into user/plan/key state.

Idempotent (a replayed webhook never double-provisions) and defensive (an event
for a variant we don't sell, or without an email, is ignored rather than trusted).
The plaintext API key is generated only at claim time — see `claim_key`.
"""
from __future__ import annotations

import hashlib
import logging

from app import db
from app.billing.lemonsqueezy import LS_STATUS, is_data_pass_variant, plan_for_variant
from app.security import generate_key

log = logging.getLogger("pakdata.billing")

_SUBSCRIPTION_EVENTS = {
    "subscription_created", "subscription_updated", "subscription_cancelled",
    "subscription_resumed", "subscription_expired", "subscription_paused",
    "subscription_unpaused", "subscription_payment_failed",
    "subscription_payment_success",
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_subscription(payload: dict) -> dict:
    """Pure extraction of the fields we care about from a LS webhook body."""
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    custom = meta.get("custom_data") or {}
    return {
        "event_name": meta.get("event_name"),
        "signup_token": custom.get("signup_token"),
        "subscription_id": data.get("id"),
        "customer_id": attrs.get("customer_id"),
        "variant_id": attrs.get("variant_id"),
        "email": (attrs.get("user_email") or "").strip().lower() or None,
        "ls_status": attrs.get("status"),
        "period_end": attrs.get("ends_at") or attrs.get("renews_at"),
    }


def already_processed(idempotency_key: str, event_name: str) -> bool:
    """Record this webhook; return True if it was already seen (a replay)."""
    n = db.execute(
        "INSERT INTO billing_events (idempotency_key, event_name) VALUES (%s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (idempotency_key, event_name),
    )
    return n == 0


PASS_DAYS = 30
_ORDER_EVENTS = {"order_created", "order_refunded"}


def parse_order(payload: dict) -> dict:
    meta = payload.get("meta") or {}
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    item = attrs.get("first_order_item") or {}
    custom = meta.get("custom_data") or {}
    return {
        "event_name": meta.get("event_name"),
        "order_id": str(data.get("id") or ""),
        "variant_id": item.get("variant_id"),
        "email": (attrs.get("user_email") or "").strip().lower() or None,
        "status": (attrs.get("status") or "").lower(),
        "refunded": bool(attrs.get("refunded")) or (attrs.get("status") or "").lower() == "refunded",
        "user_id": custom.get("user_id") or None,
    }


def _recompute_pass(user_id: str) -> None:
    """users.pass_expires_at = the latest live (not refunded) pass, or NULL."""
    db.execute(
        "UPDATE users SET pass_expires_at = (SELECT max(expires_at) FROM data_passes "
        "WHERE user_id = %(u)s AND refunded_at IS NULL) WHERE id = %(u)s", {"u": user_id})


def handle_order(payload: dict) -> dict:
    """The one-time Data pass: an order for the pass variant gives the buying account 30 days of
    the pass (added after any pass still running); a refund takes it back. Orders for anything
    else are ignored (subscriptions arrive as subscription_* events)."""
    o = parse_order(payload)
    if not is_data_pass_variant(o["variant_id"]):
        return {"handled": False, "reason": "not a data pass order"}
    user = None
    if o["user_id"]:
        user = db.query_one("SELECT id, email FROM users WHERE id::text = %s", (o["user_id"],))
    if user is None and o["email"]:
        user = db.query_one("SELECT id, email FROM users WHERE email = %s", (o["email"],))
    if user is None or not o["order_id"]:
        # The website only sells the pass to a signed-in account, so this is an edge case (a
        # checkout link shared elsewhere): tell the owner to attach it by hand.
        log.warning("billing: data pass order %s without a user", o["order_id"])
        try:
            from app.services.email import send_admin_notification
            send_admin_notification("Data pass order without an account",
                                    f"<p>Order {o['order_id']} ({o['email']}) matched no account.</p>")
        except Exception:  # noqa: BLE001
            pass
        return {"handled": False, "reason": "no user for order"}
    uid = str(user["id"])
    if o["event_name"] == "order_refunded" or o["refunded"]:
        db.execute("UPDATE data_passes SET refunded_at = now() WHERE order_id = %s AND refunded_at IS NULL",
                   (o["order_id"],))
        _recompute_pass(uid)
        return {"handled": True, "user_id": uid, "pass": "refunded"}
    if o["status"] and o["status"] not in ("paid", "completed"):
        return {"handled": False, "reason": f"order status {o['status']}"}
    db.execute(
        """INSERT INTO data_passes (order_id, user_id, days, starts_at, expires_at)
           SELECT %(o)s, %(u)s, %(d)s, s.start, s.start + make_interval(days => %(d)s)
           FROM (SELECT greatest(now(), coalesce((SELECT max(expires_at) FROM data_passes
                                                  WHERE user_id = %(u)s AND refunded_at IS NULL), now())) AS start) s
           ON CONFLICT (order_id) DO NOTHING""",
        {"o": o["order_id"], "u": uid, "d": PASS_DAYS})
    _recompute_pass(uid)
    row = db.query_one("SELECT pass_expires_at FROM users WHERE id = %s", (uid,))
    try:
        from app.services.email import send_admin_notification
        send_admin_notification("Data pass sold",
                                f"<p><b>{user['email']}</b> — Data pass, until {row['pass_expires_at']:%Y-%m-%d}.</p>")
    except Exception:  # noqa: BLE001
        log.warning("billing: admin notification failed", exc_info=True)
    return {"handled": True, "user_id": uid, "pass_expires_at": row["pass_expires_at"].isoformat()}


def handle_event(payload: dict) -> dict:
    """Apply a subscription webhook (or a Data pass order) to user state. Returns a small status dict."""
    s = parse_subscription(payload)
    if s["event_name"] in _ORDER_EVENTS:
        return handle_order(payload)
    if s["event_name"] not in _SUBSCRIPTION_EVENTS:
        return {"handled": False, "reason": "ignored event"}
    plan = plan_for_variant(s["variant_id"])
    status = LS_STATUS.get((s["ls_status"] or "").lower())
    if not plan or not s["email"] or not status:
        log.warning("billing: skipping event %s (plan=%s email=%s status=%s)",
                    s["event_name"], plan, s["email"], s["ls_status"])
        return {"handled": False, "reason": "unmapped variant/email/status"}

    row = db.query_one(
        """
        INSERT INTO users (email, plan, subscription_status, subscription_id,
                           billing_customer_id, period_end)
        VALUES (%(email)s, %(plan)s, %(status)s, %(sub)s, %(cust)s, %(pend)s)
        ON CONFLICT (email) DO UPDATE SET
            -- a plan change ends grandfathered (pre-2026-11-20) limits
            limits_version = CASE WHEN users.plan IS DISTINCT FROM EXCLUDED.plan THEN 2
                                  ELSE users.limits_version END,
            plan = EXCLUDED.plan,
            subscription_status = EXCLUDED.subscription_status,
            subscription_id = EXCLUDED.subscription_id,
            billing_customer_id = EXCLUDED.billing_customer_id,
            period_end = EXCLUDED.period_end
        RETURNING id
        """,
        {"email": s["email"], "plan": plan, "status": status,
         "sub": s["subscription_id"], "cust": str(s["customer_id"] or "") or None,
         "pend": s["period_end"]},
    )
    user_id = str(row["id"])

    # On activation, stage a one-time key claim (idempotent per signup token) so
    # the buyer can retrieve exactly one key. No claim without a signup token
    # (checkout must go through /v1/checkout) and none once the user already has
    # a live key.
    claimed = False
    if status == "active" and s["signup_token"]:
        has_key = db.query_one(
            "SELECT 1 FROM api_keys WHERE user_id=%s AND revoked_at IS NULL LIMIT 1",
            (user_id,),
        )
        if not has_key:
            db.execute(
                "INSERT INTO key_claims (token_hash, user_id) VALUES (%s, %s) "
                "ON CONFLICT (token_hash) DO NOTHING",
                (_hash(s["signup_token"]), user_id),
            )
            claimed = True

    try:
        from app.services.email import send_admin_notification
        send_admin_notification(
            f"Subscription: {s['event_name']}",
            f"<p><b>{s['email']}</b> — plan <b>{plan}</b>, status <b>{status}</b>."
            + (f" Subscription {s['subscription_id']}." if s['subscription_id'] else "") + "</p>",
        )
    except Exception:  # noqa: BLE001 — a notification must never break provisioning
        log.warning("billing: admin notification failed", exc_info=True)

    return {"handled": True, "user_id": user_id, "plan": plan,
            "status": status, "key_staged": claimed}


def claim_key(token: str) -> str | None:
    """Redeem a signup token for a freshly-generated API key, exactly once.

    The plaintext is generated here and returned to the caller only — never
    stored. Returns None if the token is unknown or already claimed."""
    token_hash = _hash(token)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE key_claims SET claimed_at = now() "
                "WHERE token_hash = %s AND claimed_at IS NULL RETURNING user_id",
                (token_hash,),
            )
            row = cur.fetchone()
            if not row:
                return None
            user_id = row[0]
            plaintext, key_hash = generate_key()
            cur.execute(
                "INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s, %s, %s)",
                (key_hash, user_id, "auto-provisioned"),
            )
    return plaintext
