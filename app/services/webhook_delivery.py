"""Webhook delivery: POST a signed payload to subscribers when a series updates.

Best-effort and defensive — a failing or slow subscriber URL must never affect
ingestion. Deliveries are HMAC-SHA256 signed with each webhook's own secret so
receivers can verify authenticity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import httpx

from app import db

log = logging.getLogger("pakdata.webhooks")

DELIVERY_TIMEOUT = 8.0


def sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _json_default(o: Any) -> Any:
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(f"not serializable: {type(o)}")


def _matching_webhooks(series_ids: list[str]) -> list[dict]:
    """Active webhooks subscribed to any of these series directly or via module."""
    return db.query(
        """
        SELECT DISTINCT w.id, w.url, w.secret, w.series_id, w.module
        FROM webhooks w
        LEFT JOIN series s ON s.id = ANY(%(ids)s) AND s.module = w.module
        WHERE w.is_active
          AND ( w.series_id = ANY(%(ids)s)
                OR (w.module IS NOT NULL AND s.id IS NOT NULL) )
        """,
        {"ids": series_ids},
    )


def _latest_for(series_id: str) -> dict | None:
    row = db.query_one(
        "SELECT obs_date, value, dims FROM observations "
        "WHERE series_id = %(id)s ORDER BY obs_date DESC LIMIT 1",
        {"id": series_id},
    )
    if not row:
        return None
    return {"date": row["obs_date"], "value": row["value"], "dims": row["dims"]}


def _series_module(series_ids: list[str]) -> dict[str, str]:
    rows = db.query("SELECT id, module FROM series WHERE id = ANY(%(ids)s)", {"ids": series_ids})
    return {r["id"]: r["module"] for r in rows}


def deliver_for_series(series_ids: Iterable[str]) -> int:
    """POST a signed update to every webhook subscribed to any of `series_ids`.

    Returns the number of successful deliveries. Never raises."""
    ids = sorted({s for s in series_ids if s})
    if not ids:
        return 0
    try:
        hooks = _matching_webhooks(ids)
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook lookup failed: %s", exc)
        return 0
    if not hooks:
        return 0

    modules = _series_module(ids)
    delivered = 0
    for h in hooks:
        # Which of the updated series does this webhook actually cover?
        matched = [
            sid for sid in ids
            if (h["series_id"] and sid == h["series_id"])
            or (h["module"] and modules.get(sid) == h["module"])
        ]
        if not matched:
            continue
        series_payload = [
            {"series_id": sid, "latest": _latest_for(sid)} for sid in matched
        ]
        body = json.dumps(
            {
                "event": "series.updated",
                "delivered_at": datetime.now(timezone.utc).isoformat(),
                "series": series_payload,
            },
            default=_json_default,
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "X-PakData-Signature": sign(body, h["secret"]),
        }
        status = None
        try:
            resp = httpx.post(h["url"], content=body, headers=headers, timeout=DELIVERY_TIMEOUT)
            status = resp.status_code
            if resp.is_success:
                delivered += 1
        except Exception as exc:  # noqa: BLE001
            log.info("webhook %s delivery failed: %s", h["id"], exc)
        try:
            db.execute(
                "UPDATE webhooks SET last_delivery_at = now(), last_status = %(st)s WHERE id = %(id)s",
                {"st": status, "id": h["id"]},
            )
        except Exception:  # noqa: BLE001
            pass
    return delivered
