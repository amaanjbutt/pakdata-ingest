"""Billing endpoints: plans, self-serve checkout, webhook, one-time key claim.

Flow:
  1. GET  /v1/plans                     -> pricing + limits (public)
  2. POST /v1/checkout {plan}           -> hosted LemonSqueezy checkout URL
  3. (buyer pays; LS -> POST /webhooks/lemonsqueezy) -> user provisioned + key staged
  4. buyer lands on /welcome?token=...  -> GET /v1/account/key/{token} once
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.billing import lemonsqueezy, service
from app.security import PLAN_LIMITS, Caller, authenticate_session_or_key

router = APIRouter(tags=["billing"])


# Public pricing (PRODUCT.md §7). Free is $0 and self-serve (no checkout).
PLAN_PRICES = {"free": 0, "developer": 19, "pro": 49, "business": 199}
PAID_PLANS = ("developer", "pro", "business")


# What each tier sells (value, not volume — 2026-10-04). Request numbers stay in
# the response but are fair-use ceilings on paid plans, not the product.
PLAN_VALUE = {
    "free": {"for": "Evaluating, students, journalists", "usage_rights": "internal",
             "vintages": False, "sla": False, "support": "community"},
    "developer": {"for": "One analyst or developer", "usage_rights": "internal",
                  "vintages": False, "sla": False, "support": "email"},
    "pro": {"for": "Risk, treasury and research teams", "usage_rights": "internal",
            "vintages": True, "sla": False, "support": "priority"},
    "business": {"for": "Apps that show the data to their users", "usage_rights": "display",
                 "vintages": True, "sla": True, "support": "dedicated"},
}


@router.get("/v1/plans")
def plans():
    from app.api.webhooks import WEBHOOK_LIMITS

    out = []
    for name, price in PLAN_PRICES.items():
        lim = PLAN_LIMITS[name]
        out.append({
            "plan": name,
            "price_usd_month": price,
            "requests_per_day": lim.per_day,
            "requests_per_min": lim.per_min,
            # Paid request numbers are fair-use ceilings, not the thing being sold.
            "fair_use": lim.credits is None,
            # Free is metered by a monthly allowance (null on paid plans).
            "free_credits": lim.credits,
            "free_credits_period": "month" if lim.credits is not None else None,
            "history": "full" if lim.history_years is None else (
                f"{lim.history_years} year" + ("" if lim.history_years == 1 else "s")),
            "funds_and_alternate": lim.allow_pro,  # every tier; kept for compatibility
            "webhooks": WEBHOOK_LIMITS.get(name, 0),  # null = unlimited
            **PLAN_VALUE[name],
        })
    return {"success": True, "plans": out}


class CheckoutRequest(BaseModel):
    plan: str
    email: str | None = None


@router.post("/v1/checkout")
def checkout(req: CheckoutRequest):
    if req.plan not in PAID_PLANS:
        raise HTTPException(
            status_code=422,
            detail=f"plan must be one of {', '.join(PAID_PLANS)}",
        )
    signup_token = secrets.token_urlsafe(24)
    try:
        url = lemonsqueezy.create_checkout(req.plan, signup_token, req.email)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:  # noqa: BLE001 — upstream/provider error
        raise HTTPException(status_code=502, detail="checkout provider error")
    return {"success": True, "checkout_url": url}


@router.get("/v1/billing/portal")
def billing_portal(caller: Caller = Depends(authenticate_session_or_key)):
    """Return the LemonSqueezy customer-portal URL for the caller's subscription
    (update card, view invoices, cancel). 404 if they have no subscription."""
    row = db.query_one("SELECT subscription_id FROM users WHERE id = %s", (caller.user_id,))
    sub_id = row.get("subscription_id") if row else None
    if not sub_id:
        raise HTTPException(status_code=404, detail="no active subscription")
    try:
        url = lemonsqueezy.customer_portal_url(str(sub_id))
    except Exception:  # noqa: BLE001 — upstream/provider error
        raise HTTPException(status_code=502, detail="billing provider error")
    if not url:
        raise HTTPException(status_code=404, detail="portal unavailable")
    return {"success": True, "url": url}


@router.post("/webhooks/lemonsqueezy")
async def lemonsqueezy_webhook(request: Request):
    raw = await request.body()
    signature = request.headers.get("X-Signature")
    if not lemonsqueezy.verify_signature(raw, signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    import json
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid payload")

    event_name = (payload.get("meta") or {}).get("event_name", "")
    # The signature is unique per payload -> a safe idempotency key against replays.
    if service.already_processed(signature, event_name):
        return {"success": True, "duplicate": True}
    result = service.handle_event(payload)
    return {"success": True, **result}


@router.get("/v1/account/key/{token}")
def claim_key(token: str):
    plaintext = service.claim_key(token)
    if plaintext is None:
        raise HTTPException(status_code=404, detail="invalid or already-claimed token")
    # Best-effort: email the key to the buyer (they also see it once on /welcome).
    import hashlib
    from app.services.email import send_api_key_email
    row = db.query_one(
        "SELECT u.email, u.plan FROM key_claims k JOIN users u ON u.id = k.user_id "
        "WHERE k.token_hash = %s",
        (hashlib.sha256(token.encode()).hexdigest(),),
    )
    if row and row.get("email"):
        send_api_key_email(row["email"], plaintext, plan=row.get("plan") or "paid")
    return {"success": True, "api_key": plaintext,
            "note": "store this now — it is shown only once"}
