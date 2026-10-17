"""Request-log housekeeping, run by the scheduler (not registered ingestion jobs).

- `flush()` (every minute): drain the `reqlog` Redis stream written by app/services/reqlog
  into `api_requests` (one partition per UTC day, created on demand) with COPY.
- `rollup(day)` (daily): per-endpoint aggregates (`usage_daily_endpoint`), top values per
  dimension (`usage_daily_dim`) and the per-user daily totals the dashboard chart reads
  (`usage_daily` — previously written only when someone opened the dashboard).
- `archive()` (daily): raw days older than RETAIN_DAYS are exported as gzip CSV to a
  PRIVATE GitHub repo (one release per month, one asset per day), verified, then dropped.
  Without REQLOG_ARCHIVE_REPO / REQLOG_ARCHIVE_TOKEN nothing is dropped.

Every function is safe to re-run.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
from datetime import date, datetime, time as dtime, timedelta, timezone

from app import db

log = logging.getLogger(__name__)

STREAM = "reqlog"
RETAIN_DAYS = int(os.getenv("REQLOG_RETAIN_DAYS", "30"))
COLUMNS = ("ts", "user_id", "key_id", "auth", "method", "route", "subject", "status", "ms",
           "bytes", "ua", "ip_hash", "country", "cache", "page")
_made: set[date] = set()


def _redis():
    from ingestion.scheduler import get_redis
    return get_redis()


def partition_name(d: date) -> str:
    return f"api_requests_d{d:%Y%m%d}"


def ensure_partition(d: date) -> None:
    """Create the day's partition. Rows that already sit in the DEFAULT partition for that
    day are moved into it (Postgres refuses to attach a range the default already holds)."""
    if d in _made:
        return
    name = partition_name(d)
    lo = datetime.combine(d, dtime(0), timezone.utc)
    hi = lo + timedelta(days=1)
    exists = db.query_one("SELECT to_regclass(%s) AS t", (name,))
    if not exists or exists["t"] is None:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE _moved ON COMMIT DROP AS SELECT * FROM api_requests_default "
                            "WHERE ts >= %s AND ts < %s", (lo, hi))
                cur.execute("DELETE FROM api_requests_default WHERE ts >= %s AND ts < %s", (lo, hi))
                cur.execute(f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF api_requests "
                            f"FOR VALUES FROM ('{lo.isoformat()}') TO ('{hi.isoformat()}')")
                cur.execute("INSERT INTO api_requests SELECT * FROM _moved")
            conn.commit()
    _made.add(d)


def _val(col: str, v: str):
    if v == "":
        return None
    if col == "ts":
        return datetime.fromtimestamp(float(v), timezone.utc)
    if col in ("status", "bytes"):
        return int(v)
    if col == "ms":
        return float(v)
    return v


def flush(batch: int = 5000, max_batches: int = 40) -> int:
    """Move stream entries into api_requests. Entries are deleted from the stream only after
    the COPY commits, so a crash can at worst duplicate a batch, never lose one."""
    r = _redis()
    total = 0
    for _ in range(max_batches):
        entries = r.xrange(STREAM, "-", "+", count=batch)
        if not entries:
            break
        rows = [tuple(_val(c, f.get(c, "")) for c in COLUMNS) for _, f in entries]
        for d in {row[0].date() for row in rows if row[0] is not None}:
            ensure_partition(d)
        rows = [row for row in rows if row[0] is not None]
        with db.connection() as conn:
            with conn.cursor() as cur:
                with cur.copy(f"COPY api_requests ({', '.join(COLUMNS)}) FROM STDIN") as cp:
                    for row in rows:
                        cp.write_row(row)
            conn.commit()
        r.xdel(STREAM, *[eid for eid, _ in entries])
        total += len(rows)
        if len(entries) < batch:
            break
    if total:
        log.info("request_log.flush: %d rows", total)
    return total


def _day_bounds(d: date) -> dict:
    lo = datetime.combine(d, dtime(0), timezone.utc)
    return {"d": d, "lo": lo, "hi": lo + timedelta(days=1)}


_DIMS = {
    # dim -> (expression, extra filter)
    "subject": ("route || ' ' || subject", "subject <> ''"),
    "page": ("page", "page <> ''"),
    "country": ("country", "country <> ''"),
    "ua": ("ua", "true"),
    "status": ("status::text", "true"),
    "route_public": ("route", "auth = 'public'"),
}


def rollup(d: date | None = None) -> dict:
    """(Re)build the aggregates for one UTC day (default: yesterday)."""
    d = d or (datetime.now(timezone.utc).date() - timedelta(days=1))
    p = _day_bounds(d)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM usage_daily_endpoint WHERE day = %(d)s", p)
            cur.execute(
                """INSERT INTO usage_daily_endpoint
                     (day, user_id, auth, route, status_class, requests, ms_p50, ms_p95, bytes, cache_hits)
                   SELECT %(d)s, user_id, auth, route, (status / 100)::smallint, count(*),
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY ms),
                          percentile_cont(0.95) WITHIN GROUP (ORDER BY ms),
                          sum(bytes), count(*) FILTER (WHERE cache = 'hit')
                   FROM api_requests WHERE ts >= %(lo)s AND ts < %(hi)s
                   GROUP BY user_id, auth, route, (status / 100)""", p)
            endpoints = cur.rowcount
            cur.execute("DELETE FROM usage_daily_dim WHERE day = %(d)s", p)
            for dim, (expr, where) in _DIMS.items():
                cur.execute(
                    f"""INSERT INTO usage_daily_dim (day, dim, value, requests)
                        SELECT %(d)s, '{dim}', v, n FROM (
                          SELECT {expr} AS v, count(*) AS n FROM api_requests
                          WHERE ts >= %(lo)s AND ts < %(hi)s AND {where}
                          GROUP BY 1 ORDER BY 2 DESC LIMIT 500) t
                        WHERE v IS NOT NULL""", p)
            cur.execute(
                """INSERT INTO usage_daily_dim (day, dim, value, requests)
                   SELECT %(d)s, 'unique', 'ip', count(DISTINCT ip_hash) FROM api_requests
                   WHERE ts >= %(lo)s AND ts < %(hi)s AND ip_hash IS NOT NULL""", p)
            # per-user daily totals for the dashboard chart (key-authenticated calls, as the
            # rate limiter counts them); never lower a count the old path already stored
            cur.execute(
                """INSERT INTO usage_daily (user_id, day, requests)
                   SELECT user_id, %(d)s, count(*) FROM api_requests
                   WHERE ts >= %(lo)s AND ts < %(hi)s AND auth = 'key' AND user_id IS NOT NULL
                   GROUP BY user_id
                   ON CONFLICT (user_id, day) DO UPDATE
                     SET requests = GREATEST(usage_daily.requests, EXCLUDED.requests)""", p)
            users = cur.rowcount
        conn.commit()
    today = datetime.now(timezone.utc).date()
    for i in range(0, 3):
        ensure_partition(today + timedelta(days=i))
    log.info("request_log.rollup %s: %d endpoint rows, %d users", d, endpoints, users)
    return {"day": d.isoformat(), "endpoint_rows": endpoints, "users": users}


# ---- archive ------------------------------------------------------------------

def _day_partitions() -> list[date]:
    rows = db.query(
        """SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
           JOIN pg_class p ON p.oid = i.inhparent
           WHERE p.relname = 'api_requests' AND c.relname ~ '^api_requests_d[0-9]{8}$'""")
    return sorted(datetime.strptime(r["relname"][-8:], "%Y%m%d").date() for r in rows)


def export_day(d: date) -> bytes:
    """The day's raw rows as gzip CSV (header row first)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with db.connection() as conn:
            with conn.cursor() as cur:
                with cur.copy(f"COPY (SELECT * FROM {partition_name(d)} ORDER BY ts) "
                              "TO STDOUT WITH (FORMAT csv, HEADER true)") as cp:
                    for chunk in cp:
                        gz.write(bytes(chunk))
    return buf.getvalue()


class GitHubReleaseStore:
    """One release per month (`reqlog-YYYY-MM`) in a PRIVATE repo, one asset per day."""

    api = "https://api.github.com"

    def __init__(self, repo: str, token: str):
        import httpx
        self.repo = repo
        self.http = httpx.Client(timeout=120, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def _release(self, tag: str) -> dict:
        r = self.http.get(f"{self.api}/repos/{self.repo}/releases/tags/{tag}")
        if r.status_code == 404:
            r = self.http.post(f"{self.api}/repos/{self.repo}/releases", json={
                "tag_name": tag, "name": f"API request log {tag[7:]}",
                "body": "Raw PakDataHub API request log (pseudonymous: hashed IPs, no query strings)."})
        r.raise_for_status()
        return r.json()

    def put(self, d: date, name: str, data: bytes) -> bool:
        """Upload unless an asset of the same name and size is already there. True = stored."""
        rel = self._release(f"reqlog-{d:%Y-%m}")
        for a in rel.get("assets", []):
            if a["name"] == name:
                if a["size"] == len(data):
                    return True
                self.http.delete(f"{self.api}/repos/{self.repo}/releases/assets/{a['id']}").raise_for_status()
        up = rel["upload_url"].split("{")[0]
        r = self.http.post(up, params={"name": name}, content=data,
                           headers={"Content-Type": "application/gzip"})
        r.raise_for_status()
        a = r.json()
        return a.get("size") == len(data) and a.get("state") == "uploaded"


def _store():
    repo, token = os.getenv("REQLOG_ARCHIVE_REPO", ""), os.getenv("REQLOG_ARCHIVE_TOKEN", "")
    return GitHubReleaseStore(repo, token) if repo and token else None


def archive(store=None, today: date | None = None) -> list[str]:
    """Export + drop raw days older than RETAIN_DAYS. A day is dropped only after its
    upload is confirmed (and after its rollup exists, so aggregates are never lost)."""
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=RETAIN_DAYS)
    due = [d for d in _day_partitions() if d < cutoff]
    if not due:
        return []
    store = store or _store()
    if store is None:
        _warn_unconfigured(len(due))
        return []
    done: list[str] = []
    for d in due:
        if not db.query_one("SELECT 1 AS x FROM usage_daily_endpoint WHERE day = %s LIMIT 1", (d,)):
            rollup(d)
        data = export_day(d)
        name = f"api_requests_{d.isoformat()}.csv.gz"
        if not store.put(d, name, data):
            log.error("request_log.archive: upload of %s not confirmed; keeping the partition", name)
            break
        db.execute(f"DROP TABLE IF EXISTS {partition_name(d)}")
        _made.discard(d)
        done.append(name)
    log.info("request_log.archive: %d day(s) exported and dropped", len(done))
    return done


def _warn_unconfigured(n: int) -> None:
    try:
        if _redis().set("reqlog:archive_unconfigured", "1", nx=True, ex=7 * 86400):
            from ingestion import alerting
            alerting.alert(
                "request log: archive not configured",
                f"{n} day(s) of raw API request logs are older than {RETAIN_DAYS} days but "
                "REQLOG_ARCHIVE_REPO / REQLOG_ARCHIVE_TOKEN are not set, so nothing was dropped.")
    except Exception:  # noqa: BLE001
        log.warning("request_log.archive: not configured (%d days waiting)", n)
