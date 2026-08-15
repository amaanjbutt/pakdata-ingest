from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.security import Caller, authenticate_session_or_key, generate_key

router = APIRouter(prefix="/v1", tags=["account"])

MAX_KEYS = 3


@router.get("/account")
def account(caller: Caller = Depends(authenticate_session_or_key)):
    """The signed-in user's account: plan, status and counts. Auth = any of the
    user's own API keys."""
    u = db.query_one(
        "SELECT email, plan, subscription_status, period_end FROM users WHERE id = %(id)s",
        {"id": caller.user_id},
    )
    keys = db.query_one(
        "SELECT count(*) AS c FROM api_keys WHERE user_id = %(id)s AND revoked_at IS NULL",
        {"id": caller.user_id},
    )["c"]
    hooks = db.query_one(
        "SELECT count(*) AS c FROM webhooks WHERE user_id = %(id)s AND is_active",
        {"id": caller.user_id},
    )["c"]
    return {
        "success": True,
        "email": u["email"] if u else None,
        "plan": caller.plan,
        "subscription_status": u["subscription_status"] if u else None,
        "period_end": u["period_end"] if u else None,
        "active_keys": keys,
        "active_webhooks": hooks,
    }


@router.get("/keys")
def list_keys(caller: Caller = Depends(authenticate_session_or_key)):
    rows = db.query(
        "SELECT id, key_prefix, label, created_at, revoked_at FROM api_keys "
        "WHERE user_id = %(id)s ORDER BY created_at DESC",
        {"id": caller.user_id},
    )
    return {"success": True, "data": rows, "count": len(rows)}


class KeyCreate(BaseModel):
    label: str | None = None


@router.post("/keys")
def create_key(body: KeyCreate, caller: Caller = Depends(authenticate_session_or_key)):
    active = db.query_one(
        "SELECT count(*) AS c FROM api_keys WHERE user_id = %(id)s AND revoked_at IS NULL",
        {"id": caller.user_id},
    )["c"]
    if active >= MAX_KEYS:
        raise HTTPException(status_code=403, detail=f"key limit reached ({MAX_KEYS}); revoke one first")
    plaintext, key_hash = generate_key()
    prefix = plaintext[:16] + "…"
    db.execute(
        "INSERT INTO api_keys (key_hash, user_id, label, key_prefix) VALUES (%s, %s, %s, %s)",
        (key_hash, caller.user_id, body.label, prefix),
    )
    # Plaintext returned once, never stored.
    return {"success": True, "api_key": plaintext, "key_prefix": prefix,
            "note": "store this key now — it is shown only once"}


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, caller: Caller = Depends(authenticate_session_or_key)):
    n = db.execute(
        "UPDATE api_keys SET revoked_at = now() "
        "WHERE id = %(id)s AND user_id = %(u)s AND revoked_at IS NULL",
        {"id": key_id, "u": caller.user_id},
    )
    if n == 0:
        raise HTTPException(status_code=404, detail="key not found")
    return {"success": True, "revoked": key_id}
