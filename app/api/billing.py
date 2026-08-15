"""Billing endpoints: plans, self-serve checkout, webhook, one-time key claim.

Flow:
  1. GET  /v1/plans                     -> pricing + limits (public)
  2. POST /v1/checkout {plan}           -> hosted LemonSqueezy checkout URL
  3. (buyer pays; LS -> POST /webhooks/lemonsqueezy) -> user provisioned + key staged
  4. buyer lands on /welcome?token=...  -> GET /v1/account/key/{token} once
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.billing import lemonsqueezy, service
from app.security import PLAN_LIMITS

router = APIRouter(tags=["billing"])


# Public pricing (PRODUCT.md §7). Free is $0 and self-serve (no checkout).
PLAN_PRICES = {"free": 0, "developer": 19, "pro": 49, "business": 199}
PAID_PLANS = ("developer", "pro", "business")


@router.get("/v1/plans")
def plans():
    out = []
    for name, price in PLAN_PRICES.items():
        lim = PLAN_LIMITS[name]
        out.append({
            "plan": name,
            "price_usd_month": price,
            "requests_per_day": lim.per_day,
            "requests_per_min": lim.per_min,
            "history": "full" if lim.history_years is None else f"{lim.history_years} years",
            "funds_and_alternate": lim.allow_pro,
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
    return {"success": True, "api_key": plaintext,
            "note": "store this now — it is shown only once"}
