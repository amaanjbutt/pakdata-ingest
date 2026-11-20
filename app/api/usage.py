from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query

from app import db
from app.security import (Caller, authenticate_session_or_key, calls_used_today, charts_used_today,
                          download_quota, get_redis, pass_active)

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage")
def usage(caller: Caller = Depends(authenticate_session_or_key)):
    """Caller's usage today vs the plan: API calls (1 call = 1 request), site downloads,
    full-history charts. Day counters reset at 00:00 UTC."""
    day_used = calls_used_today(caller.user_id)
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
    r = get_redis()
    # The rate limiter keeps a sliding 60s window as a sorted set of request
    # timestamps (security._enforce_rate_limit); count the live members.
    try:
        minute_used = int(r.zcount(f"rl:min:{caller.user_id}", time.time() - 60, "+inf"))
    except Exception:  # noqa: BLE001
        minute_used = 0
    # Grandfathered free keys (users.legacy_free_until) keep the old monthly allowance.
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
    legacy = db.query_one("SELECT legacy_free_until, limits_version FROM users WHERE id = %s", (caller.user_id,))
    lim = caller.limits
    return {
        "success": True,
        "plan": caller.plan,
        "pass_expires_at": caller.pass_expires_at if pass_active(caller.pass_expires_at) else None,
        "day_resets": "00:00 UTC",
        "limits": {
            "per_min": lim.per_min,
            "per_day": lim.per_day,
            "calls_per_day": lim.per_day,
            "history_years": lim.history_years,
            "max_points_per_call": lim.max_page,
            "downloads_per_day": lim.downloads_per_day,
            "charts_per_day": lim.charts_per_day,
            "whole_dataset_files": lim.bulk,
            "vintages_api": lim.vintages,
            "vintage_downloads": lim.vintage_downloads,
            "credits": lim.credits,
        },
        "grandfathered": {
            "free_allowance_until": legacy["legacy_free_until"] if legacy and lim.credits is not None else None,
            "paid_limits": bool(legacy and legacy.get("limits_version") == 1 and caller.plan != "free"),
        },
        "usage": {
            "minute_used": minute_used,
            "minute_remaining": max(lim.per_min - minute_used, 0),
            "day_used": day_used,
            "day_remaining": max(lim.per_day - day_used, 0),
            "downloads": download_quota(caller),
            "charts_used": charts_used_today(caller.user_id),
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
