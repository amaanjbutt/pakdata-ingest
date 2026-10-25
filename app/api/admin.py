"""Admin panel API — operational overview + user list.

Gated to ADMIN_EMAILS (config.admin_emails): the caller must authenticate with a
session token or API key AND own an admin email. Everyone else gets 403. Powers
the session-gated /admin page.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.api.billing import PLAN_PRICES
from app.config import settings
from app.security import Caller, authenticate_session_or_key

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _admin_set() -> set[str]:
    return {e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()}


def require_admin(caller: Caller = Depends(authenticate_session_or_key)) -> Caller:
    """Allow only callers whose account email is in ADMIN_EMAILS."""
    row = db.query_one("SELECT email FROM users WHERE id = %s", (caller.user_id,))
    email = ((row or {}).get("email") or "").strip().lower()
    if not email or email not in _admin_set():
        raise HTTPException(status_code=403, detail="admin access required")
    return caller


@router.get("/overview")
def overview(_: Caller = Depends(require_admin)):
    """Headline metrics: users, signups (today/7d/30d), active paid subs by plan,
    estimated MRR, and total API requests today."""
    today = date.today()
    counts = db.query_one(
        """
        SELECT
          count(*)                                                          AS total_users,
          count(*) FILTER (WHERE created_at::date = %(today)s)              AS signups_today,
          count(*) FILTER (WHERE created_at >= %(d7)s)                      AS signups_7d,
          count(*) FILTER (WHERE created_at >= %(d30)s)                     AS signups_30d,
          count(*) FILTER (WHERE subscription_status='active' AND plan<>'free') AS active_paid
        FROM users
        """,
        {"today": today, "d7": today - timedelta(days=7), "d30": today - timedelta(days=30)},
    )
    by_plan_rows = db.query(
        "SELECT plan, count(*) AS n FROM users "
        "WHERE subscription_status='active' AND plan <> 'free' GROUP BY plan"
    )
    subs_by_plan = {r["plan"]: int(r["n"]) for r in by_plan_rows}
    mrr = sum(PLAN_PRICES.get(p, 0) * n for p, n in subs_by_plan.items())
    ru = db.query_one(
        "SELECT COALESCE(SUM(requests), 0) AS n FROM usage_daily WHERE day = %s", (today,)
    )
    return {
        "success": True,
        "users": {
            "total": int(counts["total_users"]),
            "signups_today": int(counts["signups_today"]),
            "signups_7d": int(counts["signups_7d"]),
            "signups_30d": int(counts["signups_30d"]),
            "active_paid": int(counts["active_paid"]),
        },
        "subscriptions": {"by_plan": subs_by_plan, "mrr_usd": mrr},
        "usage": {"requests_today": int(ru["n"])},
    }


@router.get("/users")
def users(limit: int = 50, _: Caller = Depends(require_admin)):
    """Most-recent accounts with plan, status, credits, active key count, last-active day."""
    limit = max(1, min(limit, 200))
    rows = db.query(
        """
        SELECT u.email, u.plan, u.subscription_status AS status, u.created_at,
               u.credits_remaining,
               (SELECT count(*) FROM api_keys k WHERE k.user_id = u.id AND k.revoked_at IS NULL) AS keys,
               (SELECT max(day) FROM usage_daily d WHERE d.user_id = u.id) AS last_active
        FROM users u
        ORDER BY u.created_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    return {"success": True, "data": rows, "count": len(rows)}


# ---- operations: ingestion, data quality, traffic, catalog, system --------------------

def _safe(sql: str, params=None) -> list[dict]:
    """A query against a table that may not exist yet (runtime-created) — empty, not a 500."""
    try:
        return db.query(sql, params)
    except Exception:  # noqa: BLE001
        return []


@router.get("/ingestion")
def ingestion(_: Caller = Depends(require_admin)):
    """Every job: where it runs, its last run and last good load, and whether it is late
    against its expected interval. Plus mandi rows landed in the last three days."""
    from ingestion.registry import JOBS
    from ingestion.schedule import MONITORED_EXTERNAL, SCHEDULES, expected_intervals

    vps = {s.job for s in SCHEDULES}
    github = {s.job for s in MONITORED_EXTERNAL}
    interval = expected_intervals()
    runs = {r["job_name"]: r for r in db.query("""
        SELECT DISTINCT ON (job_name) job_name, started_at AS last_run, status AS last_status,
               rows_upserted AS last_rows, left(error, 300) AS last_error
        FROM ingestion_runs ORDER BY job_name, started_at DESC""")}
    ok = {r["job_name"]: r for r in db.query("""
        SELECT job_name, max(started_at) FILTER (WHERE status IN ('success', 'partial') AND rows_upserted > 0)
                 AS last_load,
               max(started_at) FILTER (WHERE status IN ('success', 'partial', 'no_new_data')) AS last_ok,
               count(*) FILTER (WHERE status = 'failed' AND started_at > now() - interval '7 days') AS failures_7d
        FROM ingestion_runs GROUP BY job_name""")}
    now = datetime.now(timezone.utc)
    jobs = []
    for name in sorted(set(JOBS) | set(runs)):
        r, o = runs.get(name, {}), ok.get(name, {})
        where = "vps" if name in vps else "github" if name in github else "workstation"
        exp = interval.get(name)
        last_ok = o.get("last_ok")
        late = bool(exp and (last_ok is None or now - last_ok > exp))
        jobs.append({
            "job": name, "where": where, "expected_days": exp.days if exp else None,
            "last_run": r.get("last_run"), "last_status": r.get("last_status"), "last_rows": r.get("last_rows"),
            "last_error": r.get("last_error"), "last_ok": last_ok, "last_load": o.get("last_load"),
            "failures_7d": int(o.get("failures_7d") or 0), "late": late,
        })
    agri = db.query("""
        SELECT c.province, p.obs_date, count(*) AS rows
        FROM agri_prices p JOIN agri_commodities c USING (commodity_id)
        WHERE p.obs_date > current_date - 4 GROUP BY 1, 2 ORDER BY 2 DESC, 1""")
    return {"success": True, "jobs": jobs, "late": sum(j["late"] for j in jobs),
            "failing": sum(1 for j in jobs if j["last_status"] == "failed"), "agri_recent": agri}


@router.get("/dq")
def data_quality(_: Caller = Depends(require_admin)):
    """The data-quality audit: findings by detector and verdict, reconciliation progress per
    method, public notes, quarantined rows and automatic unit-slip fixes."""
    findings = _safe("""SELECT detector, coalesce(verdict, 'unchecked') AS verdict, count(*) AS points,
                               count(DISTINCT series_id) AS series
                        FROM dq_findings GROUP BY 1, 2 ORDER BY 1, 3 DESC""")
    recon = _safe("""SELECT method, count(*) AS series, count(*) FILTER (WHERE mismatched > 0) AS series_with_diffs,
                            sum(matched) AS matched, sum(mismatched) AS differ, sum(missing_ours) AS missing_ours,
                            sum(missing_source) AS missing_source, count(*) FILTER (WHERE error IS NOT NULL) AS errors,
                            max(checked_at) AS last_checked
                     FROM dq_series_checks GROUP BY 1 ORDER BY 2 DESC""")
    easydata_total = db.query_one("SELECT count(*) AS n FROM series WHERE is_active AND easydata_dataset_code IS NOT NULL")
    notes = _safe("SELECT count(*) AS notes, count(DISTINCT series_id) AS series FROM observation_notes")
    quarantine = {}
    for t in ("fund_navs_quarantine", "security_prices_quarantine", "auctions_quarantine", "agri_prices_quarantine",
              "obs_scale_corrections"):
        rows = _safe(f"SELECT count(*) AS n FROM {t}")
        quarantine[t] = int(rows[0]["n"]) if rows else None
    return {"success": True, "findings": findings, "reconciliation": recon,
            "easydata_series_total": int(easydata_total["n"]) if easydata_total else None,
            "notes": notes[0] if notes else None, "quarantine": quarantine}


@router.get("/traffic")
def traffic(days: int = 7, _: Caller = Depends(require_admin)):
    """API traffic from the request log: per day, per endpoint, per account, status codes,
    top series / pages / countries / client types. Past days from the rollups, today raw."""
    days = max(1, min(days, 90))
    since = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    today = """SELECT (ts AT TIME ZONE 'utc')::date AS day, user_id, auth, route, (status / 100)::smallint AS status_class,
                      1 AS requests, ms AS ms_p50, ms AS ms_p95
               FROM api_requests WHERE ts >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'"""
    past = """SELECT day, user_id, auth, route, status_class, requests, ms_p50, ms_p95 FROM usage_daily_endpoint
              WHERE day >= %(since)s AND day < (now() AT TIME ZONE 'utc')::date"""
    base = f"WITH t AS ({past} UNION ALL {today})"
    p = {"since": since}
    by_day = _safe(f"""{base} SELECT day, sum(requests)::int AS requests,
                              sum(requests) FILTER (WHERE status_class >= 4)::int AS errors,
                              sum(requests) FILTER (WHERE auth = 'key')::int AS keyed
                       FROM t GROUP BY 1 ORDER BY 1""", p)
    by_route = _safe(f"""{base} SELECT route, sum(requests)::int AS requests,
                                sum(requests) FILTER (WHERE status_class = 4)::int AS client_errors,
                                sum(requests) FILTER (WHERE status_class = 5)::int AS server_errors,
                                round((sum(ms_p50 * requests) / NULLIF(sum(requests), 0))::numeric, 1)::float AS ms_typical,
                                round(max(ms_p95)::numeric, 1)::float AS ms_p95_max
                         FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 40""", p)
    by_user = _safe(f"""{base} SELECT u.email, u.plan, sum(t.requests)::int AS requests,
                               count(DISTINCT t.route) AS endpoints
                        FROM t JOIN users u ON u.id = t.user_id
                        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 40""", p)
    dims = _safe("""SELECT dim, value, sum(requests)::int AS requests FROM usage_daily_dim
                    WHERE day >= %(since)s GROUP BY 1, 2 ORDER BY 1, 3 DESC""", p)
    top: dict[str, list] = {}
    for r in dims:
        lst = top.setdefault(r["dim"], [])
        if len(lst) < 25:
            lst.append({"value": r["value"], "requests": r["requests"]})
    raw = _safe("""SELECT count(*) AS n, min(ts) AS oldest FROM api_requests""")
    return {"success": True, "days": days, "since": since.isoformat(), "by_day": by_day, "by_route": by_route,
            "by_user": by_user, "top": top, "raw_rows": raw[0] if raw else None}


@router.get("/catalog")
def catalog(_: Caller = Depends(require_admin)):
    """Live series per module and per source, and the newest data per module."""
    modules = db.query("""SELECT module, count(*) AS series, max(last_date) AS last_date,
                                 count(*) FILTER (WHERE source = 'PakDataHub') AS derived
                          FROM series WHERE is_active GROUP BY 1 ORDER BY 2 DESC""")
    sources = db.query("SELECT source, count(*) AS series FROM series WHERE is_active GROUP BY 1 ORDER BY 2 DESC")
    other = {
        "funds_reporting": (db.query_one("""SELECT count(DISTINCT fund_id) AS n FROM fund_navs
                                            WHERE obs_date > current_date - 30""") or {}).get("n"),
        "securities": (db.query_one("SELECT count(*) AS n FROM securities") or {}).get("n"),
        "trade_codes": (_safe("SELECT count(*) AS n FROM trade_hs_codes") or [{}])[0].get("n"),
        "mandi_markets": (db.query_one("SELECT count(*) AS n FROM agri_markets") or {}).get("n"),
        "search_entities": (_safe("SELECT count(*) AS n FROM search_entities") or [{}])[0].get("n"),
    }
    return {"success": True, "modules": modules, "sources": sources, "datasets": other}


@router.get("/system")
def system(_: Caller = Depends(require_admin)):
    """Database size, the largest tables, backup tables awaiting cleanup, Redis memory,
    the request-log queue and the EasyData key expiry dates (dates only)."""
    import os

    from app.security import get_redis

    size = db.query_one("SELECT pg_size_pretty(pg_database_size(current_database())) AS pretty, "
                        "pg_database_size(current_database()) AS bytes")
    tables = db.query("""SELECT c.relname AS table, pg_total_relation_size(c.oid) AS bytes,
                                pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty
                         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                         ORDER BY 2 DESC LIMIT 20""")
    backups = db.query("""SELECT c.relname AS table, pg_size_pretty(pg_total_relation_size(c.oid)) AS pretty,
                                 pg_total_relation_size(c.oid) AS bytes
                          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                          WHERE n.nspname = 'public' AND c.relkind = 'r'
                            AND (c.relname LIKE '%%\\_bak\\_%%' OR c.relname LIKE '%%\\_bak' OR c.relname LIKE '%%staging%%')
                          ORDER BY 3 DESC""")
    redis_info = {}
    try:
        r = get_redis()
        mem = r.info("memory")
        redis_info = {"used_memory_human": mem.get("used_memory_human"), "reqlog_queue": r.xlen("reqlog")}
    except Exception:  # noqa: BLE001
        pass
    keys = []
    for part in (os.getenv("EASYDATA_KEY_EXPIRIES") or "").split(","):
        if ":" in part:
            label, d = part.split(":", 1)
            try:
                left = (date.fromisoformat(d.strip()) - date.today()).days
            except ValueError:
                left = None
            keys.append({"label": label.strip(), "expires": d.strip(), "days_left": left})
    partitions = _safe("""SELECT count(*) AS n FROM pg_inherits i JOIN pg_class p ON p.oid = i.inhparent
                          WHERE p.relname = 'api_requests'""")
    return {"success": True, "database": size, "largest_tables": tables, "backup_tables": backups,
            "redis": redis_info, "easydata_keys": keys,
            "request_log": {"partitions": partitions[0]["n"] if partitions else None,
                            "archive_configured": bool(os.getenv("REQLOG_ARCHIVE_REPO") and
                                                       os.getenv("REQLOG_ARCHIVE_TOKEN"))}}
