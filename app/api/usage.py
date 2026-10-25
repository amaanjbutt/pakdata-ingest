from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

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
    # The rate limiter keeps a sliding 60s window as a sorted set of request
    # timestamps (security._enforce_rate_limit); count the live members.
    try:
        minute_used = int(r.zcount(f"rl:min:{caller.user_id}", time.time() - 60, "+inf"))
    except Exception:  # noqa: BLE001
        minute_used = 0
    # Credit-metered (free) callers: the real cap is a monthly allowance, not a daily
    # quota. Surface the remaining balance (a stale period = a fresh allowance) and
    # when it next resets, so the dashboard can show both.
    credits_remaining = None
    credits_reset = None
    if caller.limits.credits is not None:
        crow = db.query_one(
            "SELECT CASE WHEN credits_period < date_trunc('month', now())::date "
            "THEN %s ELSE credits_remaining END AS remaining, "
            "(date_trunc('month', now()) + interval '1 month')::date AS resets "
            "FROM users WHERE id = %s",
            (caller.limits.credits, caller.user_id),
        )
        if crow:
            credits_remaining = int(crow["remaining"])
            credits_reset = crow["resets"].isoformat()
    return {
        "success": True,
        "plan": caller.plan,
        "limits": {
            "per_min": caller.limits.per_min,
            "per_day": caller.limits.per_day,
            "history_years": caller.limits.history_years,
            "credits": caller.limits.credits,
        },
        "usage": {
            "minute_used": minute_used,
            "minute_remaining": max(caller.limits.per_min - minute_used, 0),
            "day_used": day_used,
            "day_remaining": max(caller.limits.per_day - day_used, 0),
            "credits_remaining": credits_remaining,
            "credits_reset": credits_reset,
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


@router.get("/usage/endpoints")
def usage_endpoints(days: int = Query(default=30, ge=1, le=90),
                    caller: Caller = Depends(authenticate_session_or_key)):
    """The caller's API-key requests per endpoint over the last `days` days (UTC), with
    status classes and median latency: past days from the daily rollup, today from the
    raw log. Dashboard sessions are not counted."""
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    rows = db.query(
        """WITH t AS (
             SELECT route, status_class, requests, ms_p50 FROM usage_daily_endpoint
             WHERE user_id = %(id)s AND auth = 'key' AND day >= %(since)s
               AND day < (now() AT TIME ZONE 'utc')::date
             UNION ALL
             SELECT route, (status / 100)::smallint, 1, ms FROM api_requests
             WHERE user_id = %(id)s AND auth = 'key'
               AND ts >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc')
           SELECT route, sum(requests)::int AS requests,
                  sum(requests) FILTER (WHERE status_class = 2)::int AS ok,
                  sum(requests) FILTER (WHERE status_class = 4)::int AS client_errors,
                  sum(requests) FILTER (WHERE status_class = 5)::int AS server_errors,
                  round((sum(ms_p50 * requests) / NULLIF(sum(requests), 0))::numeric, 1)::float AS avg_ms
           FROM t GROUP BY route ORDER BY 2 DESC""",
        {"id": caller.user_id, "since": since},
    )
    for r in rows:
        for k in ("ok", "client_errors", "server_errors"):
            r[k] = r[k] or 0
    return {"success": True, "days": days, "since": since.isoformat(), "data": rows, "count": len(rows)}
