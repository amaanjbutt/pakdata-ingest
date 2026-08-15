from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.security import Caller, authenticate_session_or_key

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

# Max active webhooks per plan (PRODUCT.md §7). None = unlimited.
WEBHOOK_LIMITS: dict[str, int | None] = {
    "free": 0,
    "developer": 3,
    "pro": 50,
    "business": None,
}


class WebhookCreate(BaseModel):
    url: str
    series_id: str | None = None
    module: str | None = None


def _limit_for(plan: str) -> int | None:
    return WEBHOOK_LIMITS.get(plan, 0)


@router.get("")
def list_webhooks(caller: Caller = Depends(authenticate_session_or_key)):
    rows = db.query(
        "SELECT id, url, series_id, module, is_active, created_at, last_delivery_at, last_status "
        "FROM webhooks WHERE user_id = %(u)s ORDER BY created_at DESC",
        {"u": caller.user_id},
    )
    return {"success": True, "data": rows, "count": len(rows)}


@router.post("")
def create_webhook(body: WebhookCreate, caller: Caller = Depends(authenticate_session_or_key)):
    if not (body.url.startswith("https://") or body.url.startswith("http://")):
        raise HTTPException(status_code=422, detail="url must be http(s)")
    if not body.series_id and not body.module:
        raise HTTPException(status_code=422, detail="provide a series_id or a module to subscribe to")

    limit = _limit_for(caller.plan)
    if limit == 0:
        raise HTTPException(status_code=403, detail="webhooks require a paid plan")
    if limit is not None:
        n = db.query_one(
            "SELECT count(*) AS c FROM webhooks WHERE user_id = %(u)s AND is_active",
            {"u": caller.user_id},
        )["c"]
        if n >= limit:
            raise HTTPException(status_code=403, detail=f"webhook limit reached for your plan ({limit})")

    signing_secret = "whsec_" + secrets.token_urlsafe(24)
    row = db.query_one(
        "INSERT INTO webhooks (user_id, url, secret, series_id, module) "
        "VALUES (%(u)s, %(url)s, %(sec)s, %(sid)s, %(mod)s) RETURNING id",
        {"u": caller.user_id, "url": body.url, "sec": signing_secret,
         "sid": body.series_id, "mod": body.module},
    )
    # The signing secret is shown once, like an API key.
    return {"success": True, "id": str(row["id"]), "signing_secret": signing_secret,
            "note": "store this signing secret now — it is shown only once"}


@router.delete("/{webhook_id}")
def delete_webhook(webhook_id: str, caller: Caller = Depends(authenticate_session_or_key)):
    n = db.execute(
        "UPDATE webhooks SET is_active = false WHERE id = %(id)s AND user_id = %(u)s AND is_active",
        {"id": webhook_id, "u": caller.user_id},
    )
    if n == 0:
        raise HTTPException(status_code=404, detail="webhook not found")
    return {"success": True, "deleted": webhook_id}
