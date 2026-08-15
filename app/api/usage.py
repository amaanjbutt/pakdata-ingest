from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends

from app import db
from app.security import Caller, authenticate_session_or_key, get_redis

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage")
def usage(caller: Caller = Depends(authenticate_session_or_key)):
    """Caller's current usage vs. limits (PRD §6 utility)."""
    r = get_redis()
    day_used = int(r.get(f"rl:day:{caller.user_id}:{date.today().isoformat()}") or 0)
    # Persist today's count so the dashboard's 30-day chart accumulates history
    # without adding a DB write to the hot request path. Best-effort.
    try:
        db.execute(
            "INSERT INTO usage_daily (user_id, day, requests) VALUES (%s, CURRENT_DATE, %s) "
            "ON CONFLICT (user_id, day) DO UPDATE SET requests = GREATEST(usage_daily.requests, EXCLUDED.requests)",
            (caller.user_id, day_used),
        )
    except Exception:  # noqa: BLE001
        pass
    minute_used = 0
    return {
        "success": True,
        "plan": caller.plan,
        "limits": {
            "per_min": caller.limits.per_min,
            "per_day": caller.limits.per_day,
            "history_years": caller.limits.history_years,
        },
        "usage": {
            "minute_used": minute_used,
            "minute_remaining": max(caller.limits.per_min - minute_used, 0),
            "day_used": day_used,
            "day_remaining": max(caller.limits.per_day - day_used, 0),
        },
    }


@router.get("/usage/history")
def usage_history(caller: Caller = Depends(authenticate_session_or_key)):
    """Daily request counts for the last 30 days, for the dashboard chart."""
    since = date.today() - timedelta(days=29)
    rows = db.query(
        "SELECT day, requests FROM usage_daily "
        "WHERE user_id = %(id)s AND day >= %(since)s ORDER BY day",
        {"id": caller.user_id, "since": since},
    )
    return {"success": True, "data": rows, "count": len(rows)}
