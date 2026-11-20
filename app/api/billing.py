"""Billing endpoints: plans, self-serve checkout, webhook, one-time key claim.

Flow:
  1. GET  /v1/plans                     -> pricing + limits (public)
  2. POST /v1/checkout {plan, billing}  -> hosted LemonSqueezy checkout URL (the Data pass:
                                           signed in, X-Session-Token; lands on /dashboard)
  3. (buyer pays; LS -> POST /webhooks/lemonsqueezy) -> user provisioned + key staged
  4. buyer lands on /welcome?token=...  -> GET /v1/account/key/{token} once
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.billing import lemonsqueezy, service
from app.security import PLAN_LIMITS, Caller, authenticate_session_or_key, session_caller

router = APIRouter(tags=["billing"])


# Public pricing (docs/decisions.md 2026-11-20). Free is $0 and self-serve (no checkout); the
# Data pass is a one-time $9 for 30 days; subscriptions bill monthly, or yearly at 10 months.
PLAN_PRICES = {"free": 0, "pass": 9, "developer": 19, "pro": 49, "business": 199}
PAID_PLANS = ("developer", "pro", "business")
PASS_DAYS = 30
ANNUAL_MONTHS = 10  # annual = 2 months free

PLAN_VALUE = {
    "free": {"name": "Free", "for": "Students, journalists, trying it", "support": "community"},
    "pass": {"name": "Data pass", "for": "A thesis or a project, without a subscription", "support": "email"},
    "developer": {"name": "Developer", "for": "One developer or analyst", "support": "email"},
    "pro": {"name": "Pro", "for": "Teams and apps", "support": "email"},
    "business": {"name": "Business", "for": "Institutions", "support": "priority"},
}


def _history(lim) -> str:
    return "full" if lim.history_years is None else f"last {lim.history_years} years"


@router.get("/v1/plans")
def plans():
    """Every plan with its real limits. 1 call = 1 request; day counters reset at 00:00 UTC.
    Every plan reaches every dataset; every plan asks you to credit PakDataHub with a link."""
    from app.api.webhooks import WEBHOOK_LIMITS

    out = []
    for name, price in PLAN_PRICES.items():
        lim = PLAN_LIMITS[name]
        once = name == "pass"
        monthly = None if once else price
        annual_on = name in PAID_PLANS and bool(lemonsqueezy.variant_for_plan(name, "year"))
        out.append({
            "plan": name,
            "name": PLAN_VALUE[name]["name"],
            "kind": "free" if name == "free" else "one_time" if once else "subscription",
            "price_usd_month": monthly,
            "price_usd_year": price * ANNUAL_MONTHS if name in PAID_PLANS else None,
            "annual_available": annual_on,
            "price_usd_once": price if once else None,
            "pass_days": PASS_DAYS if once else None,
            "available": name == "free" or bool(lemonsqueezy.variant_for_plan(name, "month")),
            "calls_per_day": lim.per_day,
            "calls_per_min": lim.per_min,
            # older names, same numbers (kept for existing clients)
            "requests_per_day": lim.per_day,
            "requests_per_min": lim.per_min,
            "max_points_per_call": lim.max_page,
            "history": _history(lim),
            "history_years": lim.history_years,
            "downloads_per_day": lim.downloads_per_day,  # null = unlimited
            "charts_full_history": True,
            "whole_dataset_files": lim.bulk,
            "vintages_api": lim.vintages,
            "vintage_downloads": lim.vintage_downloads,
            "webhooks": WEBHOOK_LIMITS.get(name, 0),  # null = unlimited
            "sla": name == "business",
            "pkr_invoice": name == "business",
            "funds_and_alternate": lim.allow_pro,  # every tier; kept for compatibility
            "for": PLAN_VALUE[name]["for"],
            "support": PLAN_VALUE[name]["support"],
        })
    disc = lemonsqueezy.pk_discount()
    return {
        "success": True,
        "plans": out,
        "day_resets": "00:00 UTC",
        "annual": "2 months free",
        "attribution": "Every plan: credit PakDataHub with a link",
        "regional": {"PK": {"percent_off": disc["percent"]}} if disc else {},
    }


class CheckoutRequest(BaseModel):
    plan: str
    email: str | None = None
    billing: str = "month"          # month | year (subscriptions)
    country: str | None = None      # the visitor's country (the website passes Cloudflare's)


@router.post("/v1/checkout")
def checkout(req: CheckoutRequest, request: Request,
             x_session_token: str | None = Header(default=None, alias="X-Session-Token")):
    if req.plan not in (*PAID_PLANS, "pass"):
        raise HTTPException(
            status_code=422,
            detail=f"plan must be one of {', '.join((*PAID_PLANS, 'pass'))}",
        )
    if req.billing not in ("month", "year"):
        raise HTTPException(status_code=422, detail="billing must be month or year")
    user_id = None
    email = req.email
    if req.plan == "pass":
        # The pass belongs to an account: buy it signed in.
        caller = session_caller(request, x_session_token) if x_session_token else None
        if caller is None:
            raise HTTPException(status_code=401, detail="sign in (free) to buy the Data pass")
        user_id = caller.user_id
        row = db.query_one("SELECT email FROM users WHERE id = %s", (user_id,))
        email = row["email"] if row else email
    signup_token = secrets.token_urlsafe(24)
    try:
        url = lemonsqueezy.create_checkout(req.plan, signup_token, email, billing=req.billing,
                                           country=req.country, user_id=user_id)
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
