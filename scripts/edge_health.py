"""Edge health: is pakdatahub.com up for people, is the website Worker inside the free plan, and one
daily email that says so — a quiet inbox then means a healthy service, not missing alerts.

Three entry points, all run by the VPS scheduler (ingestion/scheduler.py) and by hand:

    python -m scripts.edge_health probe            # every 5 min: key pages + API endpoints
    python -m scripts.edge_health daily [--day D]  # 06:30 PKT: yesterday (UTC) from Cloudflare -> ops_edge_daily
    python -m scripts.edge_health email [--print]  # 07:00 PKT: the daily health email (--print: HTML to stdout)

probe — fetches KEY_PAGES from https://pakdatahub.com (as a browser would: the human, cached pages)
and API_ENDPOINTS from https://api.pakdatahub.com. Each result goes into an hourly Redis tally (kept
48 h, read by the email). A target failing twice in a row alerts (once per 6 h; re-armed when it
answers again). Costs ~2,900 Worker requests a day of the 100k free allowance.

daily — Cloudflare's GraphQL analytics for one UTC day (CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID in the
scheduler's .env.scheduler; unset = skipped): the Worker's invocations by outcome (`exceededResources`
= the CPU-limit kills visitors see as 503/1102, 4,400 of them on 2026-11-08 and 11-11), CPU time, and
what visitors got from pakdatahub.com by status. The Worker's own edge-cache lookups
(requestSource=edgeWorkerCacheAPI, ~30k "504" rows a day that are only cache misses) are excluded.
Alerts when CPU-limit hits pass 200 a day, visitor 5xx pass 0.5 %, or Worker requests pass 80k (the
free cap is 100k). api.pakdatahub.com is not proxied by Cloudflare (DNS only), so its health comes from
the probes.

email — yesterday's edge row, the last 24 h of probes, ingestion runs (VPS and GitHub Actions, from
`ingestion_runs`), stale jobs, the GitHub Actions workflow runs of amaanjbutt/pakdata-ingest, and how
many alerts went out, to ALERT_EMAIL_TO.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger("pakdata.edge")

SITE = os.getenv("EDGE_SITE", "https://pakdatahub.com")
API = os.getenv("EDGE_API", "https://api.pakdatahub.com")
# What people open most, one per family (all cached at the edge, so a probe is a cheap hit).
KEY_PAGES = [
    "/", "/coverage", "/series/fx.rate.avg.usd", "/data/rates", "/topics/inflation",
    "/mandi", "/trade/products", "/funds", "/docs", "/status",
]
API_ENDPOINTS = ["/health", "/v1/status", "/v1/catalog/fx.rate.avg.usd"]
# A browser-like client without crawler words (frontend/src/lib/pakdata/bot-ua.ts), so the probe
# gets the page people get.
PROBE_UA = "Mozilla/5.0 (PakDataHub uptime; +https://pakdatahub.com/status)"
PROBE_TIMEOUT = 20.0

FREE_REQUESTS_PER_DAY = 100_000
CPU_LIMIT_ALERT = int(os.getenv("EDGE_CPU_LIMIT_ALERT", "200"))
SITE_5XX_ALERT_PCT = float(os.getenv("EDGE_5XX_ALERT_PCT", "0.5"))
REQUESTS_ALERT = int(os.getenv("EDGE_REQUESTS_ALERT", "80000"))

PROBE_KEY = "pakdata:edge:probe:{hour}"      # hash per UTC hour: "<target>|n|fail|ms|max"
FAILS_KEY = "pakdata:edge:fails:{target}"     # consecutive failures
ALERTED_KEY = "pakdata:edge:alerted:{target}"
PROBE_TTL = 48 * 3600
PROBE_ALERT_TTL = 6 * 3600

GHA_REPO = os.getenv("GHA_DISPATCH_REPO", "amaanjbutt/pakdata-ingest")
GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"


# ---- probes ------------------------------------------------------------------------------------

def targets() -> list[tuple[str, str, str]]:
    """(name, kind, url) for every probe target."""
    return [(p, "page", SITE + p) for p in KEY_PAGES] + [(f"api {p}", "api", API + p) for p in API_ENDPOINTS]


def check_response(kind: str, status: int, body: str) -> str | None:
    """What is wrong with a probe answer, or None. Pure."""
    if status != 200:
        return f"HTTP {status}"
    if kind == "page":
        low = body[-4000:].lower()
        if "</html>" not in low or "<title>" not in body[:20000].lower():
            return "HTTP 200 but not a whole page"
        return None
    try:
        json.loads(body)
    except ValueError:
        return "HTTP 200 but not JSON"
    return None


def probe(redis_client, get: Callable | None = None, now: datetime | None = None) -> list[dict]:
    """Fetch every target once; tally, and alert on a second failure in a row."""
    from ingestion import alerting

    now = now or datetime.now(timezone.utc)
    hour_key = PROBE_KEY.format(hour=now.strftime("%Y%m%d%H"))
    results: list[dict] = []
    client = None
    if get is None:
        client = httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False,
                              headers={"User-Agent": PROBE_UA, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
        get = client.get
    try:
        for name, kind, url in targets():
            started = datetime.now(timezone.utc)
            try:
                r = get(url)
                ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                problem = check_response(kind, r.status_code, r.text)
                edge = r.headers.get("x-edge-cache", "")
            except httpx.HTTPError as exc:
                ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                problem, edge = f"{type(exc).__name__}", ""
            results.append({"target": name, "url": url, "ok": problem is None, "problem": problem, "ms": ms, "edge": edge})
    finally:
        if client is not None:
            client.close()

    for res in results:
        t = res["target"]
        try:
            redis_client.hincrby(hour_key, f"{t}|n", 1)
            redis_client.hincrby(hour_key, f"{t}|ms", res["ms"])
            prev_max = int(redis_client.hget(hour_key, f"{t}|max") or 0)
            if res["ms"] > prev_max:
                redis_client.hset(hour_key, f"{t}|max", res["ms"])
            if res["ok"]:
                redis_client.delete(FAILS_KEY.format(target=t), ALERTED_KEY.format(target=t))
                continue
            redis_client.hincrby(hour_key, f"{t}|fail", 1)
            fails = int(redis_client.incr(FAILS_KEY.format(target=t)))
            redis_client.expire(FAILS_KEY.format(target=t), PROBE_TTL)
        except Exception as exc:  # noqa: BLE001 — Redis down: alert on this failure alone
            log.warning("probe tally unavailable (%s)", exc)
            fails = 2 if not res["ok"] else 0
        if not res["ok"] and fails >= 2:
            try:
                first = redis_client.set(ALERTED_KEY.format(target=t), "1", nx=True, ex=PROBE_ALERT_TTL)
            except Exception:  # noqa: BLE001 — fail open
                first = True
            if first:
                alerting.alert(f"{t} is failing", f"{res['url']}: {res['problem']} ({fails} checks in a row, "
                               f"{res['ms']} ms). Probed from the VPS every 5 minutes.")
    try:
        redis_client.expire(hour_key, PROBE_TTL)
    except Exception:  # noqa: BLE001
        pass
    bad = [r for r in results if not r["ok"]]
    log.info("edge probe: %d/%d ok%s", len(results) - len(bad), len(results),
             "" if not bad else " — " + ", ".join(f"{r['target']}: {r['problem']}" for r in bad))
    return results


def probe_summary(redis_client, now: datetime | None = None, hours: int = 24) -> dict[str, dict]:
    """target -> {n, fail, avg_ms, max_ms} over the last `hours` hourly tallies."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for h in range(hours):
        key = PROBE_KEY.format(hour=(now - timedelta(hours=h)).strftime("%Y%m%d%H"))
        try:
            fields = redis_client.hgetall(key) or {}
        except Exception:  # noqa: BLE001
            fields = {}
        for field, value in fields.items():
            target, _, stat = str(field).rpartition("|")
            s = out.setdefault(target, {"n": 0, "fail": 0, "ms": 0, "max_ms": 0})
            v = int(value)
            if stat == "max":
                s["max_ms"] = max(s["max_ms"], v)
            elif stat in ("n", "fail", "ms"):
                s[stat] += v
    for s in out.values():
        s["avg_ms"] = round(s.pop("ms") / s["n"]) if s["n"] else 0
    return out


# ---- Cloudflare analytics ------------------------------------------------------------------------

DAILY_QUERY = """
query($acct: string!, $zone: string!, $since: Time!, $until: Time!) {
  viewer {
    accounts(filter: {accountTag: $acct}) {
      byStatus: workersInvocationsAdaptive(limit: 50, filter: {scriptName: "pakdatahub", datetime_geq: $since, datetime_lt: $until}) {
        sum { requests errors subrequests }
        dimensions { status }
      }
      cpu: workersInvocationsAdaptive(limit: 1, filter: {scriptName: "pakdatahub", datetime_geq: $since, datetime_lt: $until}) {
        quantiles { cpuTimeP50 cpuTimeP99 }
      }
    }
    zones(filter: {zoneTag: $zone}) {
      site: httpRequestsAdaptiveGroups(limit: 100, filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball", clientRequestHTTPHost: "pakdatahub.com"}) {
        count avg { sampleInterval } dimensions { edgeResponseStatus }
      }
      api: httpRequestsAdaptiveGroups(limit: 100, filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball", clientRequestHTTPHost: "api.pakdatahub.com"}) {
        count dimensions { edgeResponseStatus }
      }
      top5xx: httpRequestsAdaptiveGroups(limit: 10, orderBy: [count_DESC], filter: {datetime_geq: $since, datetime_lt: $until, requestSource: "eyeball", edgeResponseStatus_geq: 500, clientRequestHTTPHost_in: ["pakdatahub.com", "api.pakdatahub.com"]}) {
        count dimensions { clientRequestHTTPHost clientRequestPath edgeResponseStatus }
      }
    }
  }
}
"""


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def cf_configured() -> bool:
    return all(_env(k) for k in ("CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_ZONE_ID"))


def fetch_day(day: date, post: Callable = httpx.post) -> dict:
    """The raw GraphQL answer for one UTC day. Raises on transport or GraphQL errors."""
    since = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    variables = {"acct": _env("CF_ACCOUNT_ID"), "zone": _env("CF_ZONE_ID"),
                 "since": since.isoformat().replace("+00:00", "Z"),
                 "until": (since + timedelta(days=1)).isoformat().replace("+00:00", "Z")}
    r = post(GRAPHQL, json={"query": DAILY_QUERY, "variables": variables},
             headers={"Authorization": f"Bearer {_env('CF_API_TOKEN')}"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(f"Cloudflare GraphQL: {json.dumps(body['errors'])[:500]}")
    return body


def summarize(body: dict) -> dict:
    """One day's figures from the GraphQL answer. Pure."""
    acct = (body.get("data") or {}).get("viewer", {}).get("accounts") or [{}]
    zone = (body.get("data") or {}).get("viewer", {}).get("zones") or [{}]
    a, z = acct[0] or {}, zone[0] or {}
    by_status: dict[str, int] = {}
    errors = 0
    for row in a.get("byStatus") or []:
        st = row["dimensions"]["status"]
        by_status[st] = by_status.get(st, 0) + int(row["sum"]["requests"])
        errors += int(row["sum"].get("errors") or 0)
    q = ((a.get("cpu") or [{}])[0] or {}).get("quantiles") or {}

    def weighted(rows: list[dict]) -> dict[int, int]:
        out: dict[int, int] = {}
        for row in rows or []:
            st = int(row["dimensions"]["edgeResponseStatus"])
            interval = float(((row.get("avg") or {}).get("sampleInterval")) or 1)
            out[st] = out.get(st, 0) + round(int(row["count"]) * interval)
        return out

    site = weighted(z.get("site"))
    api = weighted(z.get("api"))
    return {
        "worker_requests": sum(by_status.values()),
        "worker_errors": errors,
        "exceeded_resources": by_status.get("exceededResources", 0),
        "client_disconnected": by_status.get("clientDisconnected", 0),
        "cpu_p50_ms": round(q["cpuTimeP50"] / 1000, 1) if q.get("cpuTimeP50") is not None else None,
        "cpu_p99_ms": round(q["cpuTimeP99"] / 1000, 1) if q.get("cpuTimeP99") is not None else None,
        "site_requests": sum(site.values()),
        "site_5xx": sum(v for k, v in site.items() if k >= 500),
        "api_requests": sum(api.values()),
        "api_5xx": sum(v for k, v in api.items() if k >= 500),
        "worker_by_status": by_status,
        "site_by_status": {str(k): v for k, v in sorted(site.items())},
        "top_5xx": [
            {"host": r["dimensions"]["clientRequestHTTPHost"], "path": r["dimensions"]["clientRequestPath"],
             "status": int(r["dimensions"]["edgeResponseStatus"]), "count": int(r["count"])}
            for r in z.get("top5xx") or []
        ],
    }


def problems(s: dict) -> list[str]:
    """The day's figures that need a look. Pure."""
    out = []
    if s["exceeded_resources"] > CPU_LIMIT_ALERT:
        out.append(f"{s['exceeded_resources']:,} Worker requests hit the free plan's CPU limit "
                   f"(visitors got a 503; threshold {CPU_LIMIT_ALERT}).")
    if s["site_requests"] and 100.0 * s["site_5xx"] / s["site_requests"] > SITE_5XX_ALERT_PCT:
        out.append(f"{s['site_5xx']:,} of {s['site_requests']:,} visitor requests to pakdatahub.com failed "
                   f"with a 5xx ({100.0 * s['site_5xx'] / s['site_requests']:.2f} %, threshold {SITE_5XX_ALERT_PCT} %).")
    if s["worker_requests"] > REQUESTS_ALERT:
        out.append(f"{s['worker_requests']:,} Worker requests ({100 * s['worker_requests'] / FREE_REQUESTS_PER_DAY:.0f} % "
                   f"of the free plan's {FREE_REQUESTS_PER_DAY:,} a day).")
    return out


def store_day(day: date, s: dict) -> None:
    from app import db

    db.execute(
        """
        INSERT INTO ops_edge_daily (day, worker_requests, worker_errors, exceeded_resources, client_disconnected,
            cpu_p50_ms, cpu_p99_ms, site_requests, site_5xx, api_requests, api_5xx,
            worker_by_status, site_by_status, top_5xx, fetched_at)
        VALUES (%(day)s, %(worker_requests)s, %(worker_errors)s, %(exceeded_resources)s, %(client_disconnected)s,
            %(cpu_p50_ms)s, %(cpu_p99_ms)s, %(site_requests)s, %(site_5xx)s, %(api_requests)s, %(api_5xx)s,
            %(worker_by_status)s::jsonb, %(site_by_status)s::jsonb, %(top_5xx)s::jsonb, now())
        ON CONFLICT (day) DO UPDATE SET
            worker_requests = EXCLUDED.worker_requests, worker_errors = EXCLUDED.worker_errors,
            exceeded_resources = EXCLUDED.exceeded_resources, client_disconnected = EXCLUDED.client_disconnected,
            cpu_p50_ms = EXCLUDED.cpu_p50_ms, cpu_p99_ms = EXCLUDED.cpu_p99_ms,
            site_requests = EXCLUDED.site_requests, site_5xx = EXCLUDED.site_5xx,
            api_requests = EXCLUDED.api_requests, api_5xx = EXCLUDED.api_5xx,
            worker_by_status = EXCLUDED.worker_by_status, site_by_status = EXCLUDED.site_by_status,
            top_5xx = EXCLUDED.top_5xx, fetched_at = now()
        """,
        {**s, "day": day, "worker_by_status": json.dumps(s["worker_by_status"]),
         "site_by_status": json.dumps(s["site_by_status"]), "top_5xx": json.dumps(s["top_5xx"])},
    )


def daily(day: date | None = None, alert: bool = True) -> dict | None:
    """Pull one UTC day (default: yesterday) into ops_edge_daily and alert on its problems."""
    from ingestion import alerting

    if not cf_configured():
        log.info("edge daily: CF_API_TOKEN / CF_ACCOUNT_ID / CF_ZONE_ID not set — skipped")
        return None
    day = day or (datetime.now(timezone.utc).date() - timedelta(days=1))
    s = summarize(fetch_day(day))
    store_day(day, s)
    found = problems(s)
    log.info("edge daily %s: %s worker requests, %s CPU-limit, %s/%s site 5xx%s", day, s["worker_requests"],
             s["exceeded_resources"], s["site_5xx"], s["site_requests"], f" — {len(found)} problem(s)" if found else "")
    if found and alert:
        top = "\n".join(f"  {t['count']:>5}  {t['status']}  {t['host']}{t['path']}" for t in s["top_5xx"][:8])
        alerting.alert(f"website edge problems on {day}", "\n".join(found) + (f"\n\nTop 5xx:\n{top}" if top else ""))
    return s


# ---- the daily email -----------------------------------------------------------------------------

def _runs_last_24h() -> list[dict]:
    from app import db

    return db.query(
        """
        SELECT job_name,
               count(*) AS runs,
               count(*) FILTER (WHERE status = 'failed') AS failed,
               count(*) FILTER (WHERE status = 'partial') AS partial,
               (array_agg(status ORDER BY started_at DESC))[1] AS latest_status,
               (array_agg(error ORDER BY started_at DESC))[1] AS latest_error
        FROM ingestion_runs
        WHERE started_at > now() - interval '24 hours'
        GROUP BY job_name ORDER BY job_name
        """
    )


def _gha_runs_last_24h(get: Callable = httpx.get) -> list[dict] | None:
    """Workflow runs of the public runner repo in the last 24 h, or None without a token."""
    from ingestion import gha_dispatch

    tok = gha_dispatch.token()
    if not tok:
        return None
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = get(f"https://api.github.com/repos/{GHA_REPO}/actions/runs",
            params={"created": f">={since}", "per_page": 100},
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pakdatahub-scheduler"}, timeout=30)
    r.raise_for_status()
    return [{"name": w.get("name"), "status": w.get("status"), "conclusion": w.get("conclusion"),
             "event": w.get("event"), "url": w.get("html_url"), "created_at": w.get("created_at")}
            for w in r.json().get("workflow_runs", [])]


def _alerts_last_24h(redis_client, now: datetime | None = None) -> dict[str, int]:
    from ingestion import alerting

    now = now or datetime.now(timezone.utc)
    out: dict[str, int] = {}
    for h in range(24):
        key = alerting.COUNT_KEY.format(hour=(now - timedelta(hours=h)).strftime("%Y%m%d%H"))
        try:
            for title, n in (redis_client.hgetall(key) or {}).items():
                out[title] = out.get(title, 0) + int(n)
        except Exception:  # noqa: BLE001
            pass
    return out


def gather(redis_client) -> dict:
    """Everything the email shows. Each part fails on its own (shown as unavailable)."""
    from app import db
    from ingestion.schedule import MONITORED_EXTERNAL
    from ingestion.scheduler import stale_report

    data: dict = {"day": datetime.now(timezone.utc).date() - timedelta(days=1)}
    try:
        row = db.query_one("SELECT * FROM ops_edge_daily WHERE day = %s", (data["day"],))
        if row is None and cf_configured():
            daily(data["day"], alert=False)
            row = db.query_one("SELECT * FROM ops_edge_daily WHERE day = %s", (data["day"],))
        data["edge"] = row
    except Exception as exc:  # noqa: BLE001
        data["edge_error"] = str(exc)[:300]
    data["probes"] = probe_summary(redis_client)
    try:
        data["runs"] = _runs_last_24h()
        data["stale"] = {job: detail for job, (_, detail) in stale_report().items()}
    except Exception as exc:  # noqa: BLE001
        data["runs_error"] = str(exc)[:300]
    data["external_jobs"] = sorted({s.job for s in MONITORED_EXTERNAL})
    try:
        data["gha"] = _gha_runs_last_24h()
    except Exception as exc:  # noqa: BLE001
        data["gha_error"] = str(exc)[:300]
    data["alerts"] = _alerts_last_24h(redis_client)
    return data


def _e(v: object) -> str:
    return html.escape(str(v))


def _table(head: list[str], rows: list[list[object]]) -> str:
    th = "".join(f'<th style="text-align:left;padding:4px 10px 4px 0;border-bottom:1px solid #ddd">{_e(h)}</th>' for h in head)
    trs = "".join("<tr>" + "".join(f'<td style="padding:4px 10px 4px 0;border-bottom:1px solid #eee">{c}</td>' for c in r) + "</tr>"
                  for r in rows)
    return f'<table style="border-collapse:collapse;font-size:13px">{"<tr>" + th + "</tr>"}{trs}</table>'


def render_email(data: dict) -> tuple[str, str, int]:
    """(subject, html, number of issues). Pure."""
    issues: list[str] = []
    parts: list[str] = []

    edge = data.get("edge")
    if edge:
        s = dict(edge)
        for k in ("worker_by_status", "site_by_status", "top_5xx"):
            if isinstance(s.get(k), str):
                s[k] = json.loads(s[k])
        issues += problems(s)
        rate = 100.0 * s["site_5xx"] / s["site_requests"] if s["site_requests"] else 0.0
        rows = [
            ["Worker requests", f"{s['worker_requests']:,} ({100 * s['worker_requests'] / FREE_REQUESTS_PER_DAY:.0f} % of the 100k free allowance)"],
            ["CPU-limit hits (503)", f"{s['exceeded_resources']:,}"],
            ["Visitor requests to pakdatahub.com", f"{s['site_requests']:,}"],
            ["Visitor 5xx", f"{s['site_5xx']:,} ({rate:.2f} %)"],
            ["CPU per request, p50 / p99", f"{s.get('cpu_p50_ms') or '—'} / {s.get('cpu_p99_ms') or '—'} ms"],
        ]
        parts.append(f"<h3>Website, {_e(data['day'])} (UTC)</h3>" + _table(["", ""], [[_e(a), _e(b)] for a, b in rows]))
        if s.get("top_5xx"):
            parts.append("<p>Most frequent 5xx:</p>" + _table(
                ["Requests", "Status", "URL"],
                [[_e(t["count"]), _e(t["status"]), _e(f"{t['host']}{t['path']}"[:120])] for t in s["top_5xx"][:8]]))
    elif data.get("edge_error"):
        parts.append(f"<h3>Website</h3><p>Cloudflare figures unavailable: {_e(data['edge_error'])}</p>")
    else:
        parts.append("<h3>Website</h3><p>No Cloudflare figures (CF_API_TOKEN not set on the scheduler).</p>")

    probes = data.get("probes") or {}
    if probes:
        failing = {t: p for t, p in probes.items() if p["fail"]}
        for t, p in failing.items():
            if p["fail"] >= 2:
                issues.append(f"{t}: {p['fail']} of {p['n']} uptime checks failed.")
        rows = [[_e(t), _e(p["n"]), _e(p["fail"]) if p["fail"] else "0", _e(p["avg_ms"]), _e(p["max_ms"])]
                for t, p in sorted(probes.items(), key=lambda kv: (-kv[1]["fail"], kv[0]))]
        parts.append("<h3>Uptime checks, last 24 h (from the VPS, every 5 min)</h3>"
                     + _table(["Target", "Checks", "Failed", "Avg ms", "Max ms"], rows))
    else:
        issues.append("No uptime checks recorded in the last 24 h (is the scheduler running?).")

    if "runs" in data:
        runs = data["runs"]
        external = set(data.get("external_jobs") or [])
        total = sum(r["runs"] for r in runs)
        bad = [r for r in runs if r["failed"] or r["partial"]]
        for r in bad:
            if r["latest_status"] == "failed":
                issues.append(f"{r['job_name']}: latest run failed.")
        stale = data.get("stale") or {}
        for job, detail in stale.items():
            issues.append(f"{job} is late: {detail}")
        parts.append(f"<h3>Ingestion, last 24 h</h3><p>{total} runs of {len(runs)} jobs; "
                     f"{len(bad)} job(s) with a failed or partial run; {len(stale)} late.</p>")
        if bad:
            parts.append(_table(
                ["Job", "Runs on", "Runs", "Failed", "Partial", "Latest", "Latest error"],
                [[_e(r["job_name"]), "GitHub" if r["job_name"] in external else "VPS", _e(r["runs"]), _e(r["failed"]),
                  _e(r["partial"]), _e(r["latest_status"]), _e((r["latest_error"] or "")[:160])] for r in bad]))
        if stale:
            parts.append("<p>Late (no fresh run within the expected interval):</p>"
                         + _table(["Job", "Detail"], [[_e(j), _e(d)] for j, d in sorted(stale.items())]))
    else:
        parts.append(f"<h3>Ingestion</h3><p>Unavailable: {_e(data.get('runs_error', ''))}</p>")

    gha = data.get("gha")
    if gha is not None:
        failed = [w for w in gha if w["conclusion"] in ("failure", "timed_out", "startup_failure")]
        for w in failed:
            issues.append(f"GitHub Actions: {w['name']} {w['conclusion']}.")
        parts.append(f"<h3>GitHub Actions ({_e(GHA_REPO)}), last 24 h</h3><p>{len(gha)} workflow runs, "
                     f"{len(failed)} failed, {sum(1 for w in gha if w['conclusion'] == 'cancelled')} cancelled.</p>")
        if failed:
            parts.append(_table(["Workflow", "Result", "Started", "Run"],
                                [[_e(w["name"]), _e(w["conclusion"]), _e(w["created_at"]),
                                  f'<a href="{_e(w["url"])}">open</a>'] for w in failed]))
    elif data.get("gha_error"):
        parts.append(f"<h3>GitHub Actions</h3><p>Unavailable: {_e(data['gha_error'])}</p>")

    alerts = data.get("alerts") or {}
    n_alerts = sum(alerts.values())
    parts.append(f"<h3>Alerts sent, last 24 h: {n_alerts}</h3>")
    if alerts:
        parts.append(_table(["Alert", "Times"], [[_e(t), _e(n)] for t, n in sorted(alerts.items(), key=lambda kv: -kv[1])]))

    head = ("<p><b>All clear.</b> Nothing needs a look.</p>" if not issues else
            f"<p><b>{len(issues)} thing{'s' if len(issues) != 1 else ''} to look at:</b></p><ul>"
            + "".join(f"<li>{_e(i)}</li>" for i in issues) + "</ul>")
    body = ('<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:720px;color:#1a1a1a;line-height:1.5">'
            f'<h2 style="font-size:18px">PakDataHub daily health</h2>{head}{"".join(parts)}'
            '<p style="font-size:12px;color:#888;margin-top:24px">scripts/edge_health.py on the VPS scheduler, 07:00 PKT. '
            'Thresholds: CPU-limit hits &gt; 200/day, visitor 5xx &gt; 0.5 %, Worker requests &gt; 80k/day, '
            'two failed uptime checks.</p></div>')
    when = data["day"].strftime("%b %d") if isinstance(data.get("day"), date) else str(data.get("day"))
    subject = f"PakDataHub health · {when}: " + ("all clear" if not issues else f"{len(issues)} to look at")
    return subject, body, len(issues)


def daily_email(redis_client, send: bool = True) -> tuple[str, str, int]:
    from app.services.email import send_email

    subject, body, n = render_email(gather(redis_client))
    to = os.getenv("ALERT_EMAIL_TO")
    if send:
        if not to:
            log.warning("daily health email: ALERT_EMAIL_TO not set — not sent")
        elif not send_email(to, subject, body):
            log.warning("daily health email: send failed")
        else:
            log.info("daily health email sent (%d issue(s))", n)
    return subject, body, n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="scripts.edge_health")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    d = sub.add_parser("daily")
    d.add_argument("--day", type=date.fromisoformat, default=None, help="UTC day (default: yesterday)")
    e = sub.add_parser("email")
    e.add_argument("--print", action="store_true", help="print the HTML instead of sending")
    args = p.parse_args(argv)

    import redis

    from app.config import settings

    r = redis.from_url(settings.redis_url, decode_responses=True)
    if args.cmd == "probe":
        res = probe(r)
        for x in res:
            print(f"{'ok ' if x['ok'] else 'BAD'} {x['ms']:>6} ms  {x['edge']:<14} {x['target']}  {x['problem'] or ''}")
        return 0 if all(x["ok"] for x in res) else 1
    if args.cmd == "daily":
        s = daily(args.day)
        print(json.dumps(s, indent=1, default=str) if s else "skipped (no Cloudflare credentials)")
        return 0
    subject, body, n = daily_email(r, send=not args.print)
    if args.print:
        print(subject)
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
