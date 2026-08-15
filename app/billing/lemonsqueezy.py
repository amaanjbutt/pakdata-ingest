"""LemonSqueezy integration primitives (provider-specific; kept behind this
module so the provider can be swapped, per the roadmap).

LemonSqueezy is a Merchant of Record, which is why it's used here instead of
Stripe (unavailable for Pakistan-domiciled sellers).

Only pure/reusable helpers live here: webhook signature verification, mapping a
product-variant id to our plan, and creating a hosted checkout. The provisioning
side-effects live in `app.billing.service`.
"""
from __future__ import annotations

import hashlib
import hmac

import httpx

from app.config import settings

API = "https://api.lemonsqueezy.com/v1"

# LemonSqueezy subscription.status -> our users.subscription_status.
# 'cancelled' stays usable until it actually 'expires' (access to period end).
LS_STATUS = {
    "active": "active",
    "on_trial": "active",
    "cancelled": "active",
    "paused": "past_due",
    "past_due": "past_due",
    "unpaid": "past_due",
    "expired": "canceled",
}


def verify_signature(payload: bytes, signature: str | None, secret: str | None = None) -> bool:
    """Verify a LemonSqueezy webhook: HMAC-SHA256(raw body, signing secret), hex,
    compared in constant time. A missing secret or signature fails closed."""
    secret = settings.lemonsqueezy_webhook_secret if secret is None else secret
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature.strip())


# Paid plans that map to a LemonSqueezy variant. Free is provisioned without
# checkout, so it has no variant.
def _variant_map() -> dict[str, str]:
    return {
        "developer": settings.lemonsqueezy_variant_developer,
        "pro": settings.lemonsqueezy_variant_pro,
        "business": settings.lemonsqueezy_variant_business,
    }


def plan_for_variant(variant_id: str | None) -> str | None:
    """Map a LemonSqueezy product-variant id to a paid plan, or None if the
    variant isn't one we sell (defensive against foreign/misconfigured events)."""
    if variant_id is None:
        return None
    vid = str(variant_id)
    for plan, variant in _variant_map().items():
        if variant and vid == str(variant):
            return plan
    return None


def variant_for_plan(plan: str) -> str | None:
    return _variant_map().get(plan) or None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.lemonsqueezy_api_key}",
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
    }


def create_checkout(plan: str, signup_token: str, email: str | None = None) -> str:
    """Create a hosted checkout for `plan` and return its URL.

    `signup_token` is threaded through `custom_data` (so the webhook can bind the
    resulting subscription to this signup) and the success redirect (so the buyer
    can claim their key). Requires store id + the plan's variant id configured."""
    variant = variant_for_plan(plan)
    if not (settings.lemonsqueezy_api_key and settings.lemonsqueezy_store_id and variant):
        raise RuntimeError("billing not configured (api key / store / variant)")

    redirect = f"{settings.public_base_url}/welcome?token={signup_token}"
    attributes: dict = {
        "checkout_data": {"custom": {"signup_token": signup_token}},
        "product_options": {"redirect_url": redirect},
    }
    if email:
        attributes["checkout_data"]["email"] = email
    body = {
        "data": {
            "type": "checkouts",
            "attributes": attributes,
            "relationships": {
                "store": {"data": {"type": "stores", "id": str(settings.lemonsqueezy_store_id)}},
                "variant": {"data": {"type": "variants", "id": str(variant)}},
            },
        }
    }
    r = httpx.post(f"{API}/checkouts", headers=_headers(), json=body, timeout=30)
    r.raise_for_status()
    return r.json()["data"]["attributes"]["url"]
