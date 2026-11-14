"""Scheduler service (Phase 1).

Runs a single APScheduler `BlockingScheduler` in the Asia/Karachi timezone. It
invokes jobs in-process via the shared `JOBS` registry (so failures are
catchable and logged), guarantees no two instances of the same job overlap
(APScheduler `max_instances=1` *plus* a cross-process Redis lock), and adds a
small random jitter so we never hit a source at an exact tick.

A periodic `staleness_check` reads `ingestion_runs` and alerts when a job's last
successful run is older than its declared `expected_interval` — a silently
stopped job is worse than a crashing one.

    python -m ingestion.scheduler
"""
from __future__ import annotations

import logging
import os
import socket
import uuid
from datetime import date, datetime, timezone

import redis
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import settings
from ingestion import alerting
from ingestion.registry import JOBS
from ingestion.schedule import GHA_DISPATCH, SCHEDULES, expected_intervals, stale_on_run_jobs

log = logging.getLogger("pakdata.scheduler")

MAX_JITTER_SECONDS = 120
LOCK_TTL_SECONDS = int(os.getenv("JOB_LOCK_TTL", "7200"))  # 2h; covers a slow run
STALENESS_CHECK_HOURS = 1
# A run still 'running' after this long is presumed dead (killed process, crashed
# container). Long backfills legitimately run for hours, so keep this generous.
STUCK_RUN_HOURS = int(os.getenv("STUCK_RUN_HOURS", "12"))
# staleness_check runs hourly; without a throttle a stale job would email every
# hour until it recovers. Alert once per job per window, re-arm on recovery.
STALE_ALERT_TTL = int(os.getenv("STALE_ALERT_TTL", str(24 * 3600)))

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ---- cross-process lock ------------------------------------------------------

def _lock_key(job_name: str) -> str:
    return f"pakdata:joblock:{job_name}"


def _acquire(job_name: str) -> str | None:
    """Try to take the lock. Returns a token on success, None if already held."""
    token = uuid.uuid4().hex
    ok = get_redis().set(_lock_key(job_name), token, nx=True, ex=LOCK_TTL_SECONDS)
    return token if ok else None


def _release(job_name: str, token: str) -> None:
    # Only release if we still own it (compare-and-delete via Lua).
    lua = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        get_redis().eval(lua, 1, _lock_key(job_name), token)
    except Exception as exc:  # never let lock cleanup crash the run
        log.warning("lock release failed for %s: %s", job_name, exc)


# ---- job invocation ----------------------------------------------------------

def run_job(job_name: str) -> None:
    """Invoke a registered job with overlap protection. Errors are logged and
    alerted (the framework already alerts on failure) but never propagate, so
    one bad run can't take the scheduler down."""
    job_cls = JOBS.get(job_name)
    if job_cls is None:
        log.error("scheduled job %r is not registered; skipping", job_name)
        return

    # Global single-flight: never let a scheduled job pile onto a running job
    # (scheduled or a manual backfill) and overload the small box.
    from ingestion.joblock import GlobalJobLock

    glock = GlobalJobLock(owner=f"scheduler:{job_name}")
    if not glock.try_acquire():
        log.warning("%s skipped this tick: another ingestion job is running (%s)",
                    job_name, glock.holder())
        return

    token = _acquire(job_name)
    if token is None:
        glock.release()
        log.warning("%s already running (lock held); skipping this tick", job_name)
        return
    try:
        log.info("starting %s", job_name)
        result = job_cls().run()
        log.info("%s finished: %s", job_name, result)
    except Exception:  # framework already recorded + alerted; keep scheduler alive
        log.exception("%s raised", job_name)
    finally:
        _release(job_name, token)
        glock.release()


# ---- staleness detection -----------------------------------------------------

def _last_run_rows() -> dict[str, dict]:
    """Latest run row per job (any status)."""
    rows = db.query(
        """
        SELECT DISTINCT ON (job_name)
               job_name, status, started_at, finished_at
        FROM ingestion_runs
        ORDER BY job_name, started_at DESC
        """
    )
    return {r["job_name"]: r for r in rows}


def _last_success_at() -> dict[str, datetime]:
    # A 'partial' run only counts as fresh if it actually made progress
    # (rows_upserted > 0). A partial that ingested nothing — e.g. an EasyData run
    # that 429'd on an exhausted quota and stopped clean — must NOT mask staleness.
    rows = db.query(
        """
        SELECT DISTINCT ON (job_name) job_name, started_at
        FROM ingestion_runs
        WHERE status IN ('success', 'no_new_data')
           OR (status = 'partial' AND rows_upserted > 0)
        ORDER BY job_name, started_at DESC
        """
    )
    return {r["job_name"]: r["started_at"] for r in rows}


def stuck_run_check() -> list[int]:
    """Reap ingestion_runs wedged in 'running'.

    A killed process (container recreate, OOM, crash) never writes a terminal
    status, so its row sits at 'running' forever. That row then looks like a
    healthy in-flight job: it masks the failure from `staleness_check` (which
    only reads terminal statuses) and the job appears to be working when it
    is dead. Mark such rows failed and alert. Returns the reaped run ids.
    """
    rows = db.query(
        """
        SELECT id, job_name, started_at FROM ingestion_runs
        WHERE status = 'running'
          AND started_at < now() - (%(hours)s || ' hours')::interval
        ORDER BY id
        """,
        {"hours": STUCK_RUN_HOURS},
    )
    reaped: list[int] = []
    for r in rows:
        db.execute(
            "UPDATE ingestion_runs SET status='failed', finished_at=now(), "
            "error=%s WHERE id=%s AND status='running'",
            (f"stuck in 'running' > {STUCK_RUN_HOURS}h; presumed dead process", r["id"]),
        )
        reaped.append(r["id"])
        alerting.alert(
            f"stuck run reaped: {r['job_name']}",
            f"run {r['id']} started {r['started_at']} and never finished "
            f"(> {STUCK_RUN_HOURS}h). Marked failed — the job is NOT running.",
        )
    if not reaped:
        log.info("stuck-run check: no wedged runs")
    return reaped


def _stale_alert_key(job_name: str) -> str:
    return f"pakdata:stalealert:{job_name}"


def _alert_stale(job_name: str, title: str, msg: str) -> None:
    """Alert on a stale job at most once per STALE_ALERT_TTL. Fails OPEN: if Redis
    is unreachable we alert anyway (a missed alert is worse than a duplicate)."""
    try:
        first = get_redis().set(_stale_alert_key(job_name), "1", nx=True, ex=STALE_ALERT_TTL)
    except Exception as exc:
        log.warning("stale-alert throttle unavailable (%s); alerting anyway", exc)
        first = True
    if first:
        alerting.alert(title, msg)
    else:
        log.info("staleness: %s still stale (alert throttled)", job_name)


def _clear_stale_alert(job_name: str) -> None:
    """Re-arm the alert once a job is fresh again, so the next lapse alerts at once."""
    try:
        get_redis().delete(_stale_alert_key(job_name))
    except Exception:
        pass


def _span(td) -> str:
    """'4 d 3 h', '5 h 12 min', '40 min'."""
    mins = int(td.total_seconds() // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    if d:
        return f"{d} d {h} h" if h else f"{d} d"
    return f"{h} h {m} min" if h else f"{m} min"


def stale_report() -> dict[str, tuple[str, str]]:
    """Each registered monitored job whose freshness has lapsed -> (title, detail). Reads
    `ingestion_runs`, sends nothing (the daily health email lists the same jobs)."""
    intervals = expected_intervals()
    stale_on_run = stale_on_run_jobs()
    last_success = _last_success_at()
    last_run = _last_run_rows()
    now = datetime.now(timezone.utc)
    out: dict[str, tuple[str, str]] = {}
    for job_name, interval in intervals.items():
        if job_name not in JOBS:
            continue  # declared ahead of implementation
        # For "changes rarely" jobs, freshness = last run attempt, not last data.
        ref = last_run.get(job_name, {}).get("started_at") if job_name in stale_on_run             else last_success.get(job_name)
        if ref is None:
            out[job_name] = (f"staleness: {job_name} has never succeeded",
                             f"No successful run on record; expected every {_span(interval)}.")
        elif now - ref > interval:
            out[job_name] = (f"staleness: {job_name} is stale",
                             f"Last fresh run {_span(now - ref)} ago (expected every {_span(interval)}).")
    return out


def staleness_check() -> list[str]:
    """Alert on jobs whose freshness has lapsed. Returns the list of stale job
    names (also useful for tests). Only checks jobs that are actually
    registered — declared-but-unbuilt jobs are ignored."""
    report = stale_report()
    for job_name in expected_intervals():
        if job_name not in JOBS:
            continue
        if job_name in report:
            _alert_stale(job_name, *report[job_name])
        else:
            _clear_stale_alert(job_name)
    if not report:
        log.info("staleness check: all %d registered scheduled jobs fresh", len(expected_intervals()))
    return list(report)


def health_check() -> dict:
    """Hourly health sweep. Reaps wedged runs *first* so that a dead job stops
    masquerading as in-flight, then evaluates staleness against real statuses,
    then the box itself (disk, memory, Redis, backups)."""
    reaped = stuck_run_check()
    stale = staleness_check()
    try:
        resources = resource_check()
    except Exception as exc:  # noqa: BLE001 — the sweep must never kill the scheduler
        log.exception("resource check failed")
        resources = [f"resource check failed: {exc}"]
    try:  # GitHub Actions runs can't email; their problems surface here (ingestion/external_runs.py)
        from ingestion.external_runs import external_run_check
        external = external_run_check(get_redis())
    except Exception as exc:  # noqa: BLE001
        log.exception("external run check failed")
        external = {"check": f"failed: {exc}"}
    return {"reaped": reaped, "stale": stale, "resources": resources, "external": external}


# ---- host resources ------------------------------------------------------------
# The scheduler container sees the host's root filesystem through the rawdata volume
# (/var/lib/docker is on it, and so are pgdata and backups) and the host's /proc/meminfo,
# so it can watch the whole box. Thresholds are env-tunable; each problem alerts at most
# once per STALE_ALERT_TTL and re-arms when it clears.
DISK_PATH = os.getenv("RESOURCE_DISK_PATH", "/data/raw")
DISK_ALERT_PCT = float(os.getenv("DISK_ALERT_PCT", "80"))
MEM_ALERT_AVAILABLE_PCT = float(os.getenv("MEM_ALERT_AVAILABLE_PCT", "10"))
SWAP_ALERT_USED_PCT = float(os.getenv("SWAP_ALERT_USED_PCT", "75"))
REDIS_ALERT_PCT = float(os.getenv("REDIS_ALERT_PCT", "85"))
BACKUP_DIR = os.getenv("BACKUP_DIR", "/backups")
BACKUP_MAX_AGE_HOURS = float(os.getenv("BACKUP_MAX_AGE_HOURS", "30"))
OFFSITE_MAX_AGE_HOURS = float(os.getenv("OFFSITE_MAX_AGE_HOURS", "50"))   # one missed nightly run is fine


def _disk_usage(path: str):
    import shutil
    return shutil.disk_usage(path)


def _meminfo() -> dict[str, int]:
    """/proc/meminfo in kB ({} where it doesn't exist)."""
    out: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    out[key] = int(parts[0])
    except OSError:
        pass
    return out


def _last_backup() -> tuple[datetime, str] | None:
    """(time, what) of the last good backup, or None if BACKUP_DIR isn't mounted.

    backup.sh writes `last_ok` only after a dump passed its restore test; until the
    first such run, fall back to the newest dump file's mtime."""
    if not os.path.isdir(BACKUP_DIR):
        return None
    marker = os.path.join(BACKUP_DIR, "last_ok")
    try:
        with open(marker) as fh:
            parts = fh.read().split()
        return datetime.fromtimestamp(int(parts[0]), timezone.utc), " ".join(parts[2:3]) or "last_ok"
    except (OSError, ValueError, IndexError):
        pass
    dumps = [f for f in os.listdir(BACKUP_DIR) if f.startswith("pakdata_") and f.endswith(".dump")]
    if not dumps:
        return datetime.fromtimestamp(0, timezone.utc), "no dump at all"
    newest = max(dumps)
    mtime = os.path.getmtime(os.path.join(BACKUP_DIR, newest))
    return datetime.fromtimestamp(mtime, timezone.utc), newest


def _resource_alert(key: str, problem: str | None, title: str) -> None:
    """Alert once per STALE_ALERT_TTL while `problem` persists; re-arm when it clears."""
    if problem is None:
        _clear_stale_alert(f"resource:{key}")
    else:
        _alert_stale(f"resource:{key}", title, problem)


def resource_check() -> list[str]:
    """Disk, memory, Redis and backup freshness. Returns the problems found."""
    problems: dict[str, tuple[str | None, str]] = {}

    du = _disk_usage(DISK_PATH)
    used_pct = 100.0 * du.used / du.total
    problems["disk"] = ((
        f"Host disk {used_pct:.0f}% used ({du.free / 2**30:.1f} GB free of {du.total / 2**30:.0f} GB). "
        "docs/operations.md → Incidents → Disk filling."
    ) if used_pct >= DISK_ALERT_PCT else None, f"VPS disk {used_pct:.0f}% full")

    mem = _meminfo()
    if mem.get("MemTotal"):
        avail_pct = 100.0 * mem.get("MemAvailable", 0) / mem["MemTotal"]
        swap_total = mem.get("SwapTotal", 0)
        swap_pct = 100.0 * (swap_total - mem.get("SwapFree", 0)) / swap_total if swap_total else 0.0
        low = avail_pct < MEM_ALERT_AVAILABLE_PCT or swap_pct >= SWAP_ALERT_USED_PCT
        problems["memory"] = ((
            f"MemAvailable {mem.get('MemAvailable', 0) // 1024} MB ({avail_pct:.0f}% of "
            f"{mem['MemTotal'] // 1024} MB), swap {swap_pct:.0f}% used. Check `docker stats` and "
            "detached backfills in the api container."
        ) if low else None, f"VPS memory low ({avail_pct:.0f}% available)")

    try:
        info = get_redis().info("memory")
        cap, used = int(info.get("maxmemory") or 0), int(info.get("used_memory") or 0)
        if cap:
            pct = 100.0 * used / cap
            problems["redis"] = ((
                f"Redis {used / 2**20:.0f} MB of its {cap / 2**20:.0f} MB cap ({pct:.0f}%); keys with a "
                "TTL are being evicted. Look for a runaway cache key family (redis-cli --bigkeys)."
            ) if pct >= REDIS_ALERT_PCT else None, f"Redis memory {pct:.0f}% of cap")
    except Exception as exc:  # noqa: BLE001
        log.warning("resource check: redis info unavailable (%s)", exc)

    last = _last_backup()
    if last is not None:
        when, what = last
        age_h = (datetime.now(timezone.utc) - when).total_seconds() / 3600
        problems["backup"] = ((
            f"Last verified backup {age_h:.0f} h ago ({what}). Check `docker compose -f "
            "docker-compose.server.yml logs --tail 40 backup`."
        ) if age_h > BACKUP_MAX_AGE_HOURS else None, "DB backup is stale")

    from ingestion import offsite_backup
    if offsite_backup.configured():
        try:
            raw = get_redis().get(offsite_backup.REDIS_KEY) or ""
            ts, _, what = raw.partition(" ")
            age_h = (datetime.now(timezone.utc).timestamp() - int(ts)) / 3600 if ts.isdigit() else float("inf")
            problems["offsite"] = ((
                f"Last off-site copy {'never recorded' if age_h == float('inf') else f'{age_h:.0f} h ago'}"
                f"{f' ({what})' if what else ''}. `$DC logs scheduler | grep offsite_backup`; rerun with "
                "`$DC exec -T scheduler python -c \"from ingestion import offsite_backup as o, scheduler as s; "
                "print(o.run(redis_client=s.get_redis()))\"`."
            ) if age_h > OFFSITE_MAX_AGE_HOURS else None, "Off-site DB backup is stale")
        except Exception as exc:  # noqa: BLE001
            log.warning("resource check: off-site marker unavailable (%s)", exc)

    found: list[str] = []
    for key, (problem, title) in problems.items():
        _resource_alert(key, problem, title)
        if problem:
            found.append(f"{key}: {problem}")
    if found:
        log.warning("resource check: %s", "; ".join(found))
    else:
        log.info("resource check: disk %.0f%% used, memory/redis/backups ok", used_pct)
    return found


# EasyData API keys expire and must be rotated by hand (no auto-provision). When
# one lapses, ingestion silently degrades to 0 rows (a 401 per call). This surfaces
# it ahead of time from known expiry DATES — never key values.
_KEY_EXPIRY_WARN_DAYS = (21, 14, 7, 3, 1)


def key_expiry_check() -> list[str]:
    """Daily: warn ahead of an EasyData API key expiry, then daily once it lapses.

    Reads `EASYDATA_KEY_EXPIRIES` = comma-separated `label:YYYY-MM-DD` (expiry
    dates only, no secrets). Alerts at 21/14/7/3/1 days out and every day once
    expired. Returns the labels warned (also useful for tests)."""
    raw = os.getenv("EASYDATA_KEY_EXPIRIES", "").strip()
    if not raw:
        return []
    today = datetime.now(timezone.utc).date()
    warned: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        label, sep, datestr = part.rpartition(":")
        try:
            exp = date.fromisoformat((datestr if sep else label).strip())
        except ValueError:
            log.warning("key_expiry_check: bad entry %r (want label:YYYY-MM-DD)", part)
            continue
        name = label.strip() if sep and label.strip() else exp.isoformat()
        days = (exp - today).days
        if days < 0:
            warned.append(name)
            alerting.alert(
                f"EasyData key EXPIRED: {name}",
                f"Key '{name}' expired {-days}d ago ({exp}); EasyData ingestion is "
                "degrading. Mint a replacement and update EASYDATA_API_KEYS in BOTH "
                "GHA repos (amaanjbutt + amaanu01) and the VPS .env, plus "
                "EASYDATA_KEY_EXPIRIES here.",
            )
        elif days in _KEY_EXPIRY_WARN_DAYS:
            warned.append(name)
            alerting.alert(
                f"EasyData key expiring in {days}d: {name}",
                f"Key '{name}' expires {exp} ({days}d away). Mint a replacement "
                "before then and update EASYDATA_API_KEYS in both GHA repos + the "
                "VPS .env, plus EASYDATA_KEY_EXPIRIES here.",
            )
    if not warned:
        log.info("key_expiry_check: no EasyData key within %dd of expiry",
                 max(_KEY_EXPIRY_WARN_DAYS))
    return warned


# ---- wiring ------------------------------------------------------------------

def build_scheduler() -> BlockingScheduler:
    sched = BlockingScheduler(
        timezone=settings.timezone,
        job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 3600},
    )
    for i, s in enumerate(SCHEDULES):
        registered = s.job in JOBS
        trigger = CronTrigger(**s.cron, timezone=settings.timezone,
                              jitter=MAX_JITTER_SECONDS)
        sched.add_job(
            run_job,
            trigger=trigger,
            args=[s.job],
            id=f"{s.job}#{i}",
            name=s.job,
        )
        state = "registered" if registered else "DECLARED-ONLY (will skip)"
        log.info("scheduled %-22s cron=%s  [%s]", s.job, s.cron, state)

    sched.add_job(
        health_check,
        trigger=CronTrigger(minute=0, timezone=settings.timezone),  # hourly
        id="health_check",
        name="health_check",
    )
    log.info("scheduled %-22s cron=hourly (stuck-run reap + staleness + disk/memory/backups)", "health_check")

    sched.add_job(
        key_expiry_check,
        trigger=CronTrigger(hour=8, minute=5, timezone=settings.timezone),  # daily
        id="key_expiry_check",
        name="key_expiry_check",
    )
    log.info("scheduled %-22s cron=daily 08:05 (EasyData key-expiry warning)",
             "key_expiry_check")

    # API request log: drain the Redis stream every minute; roll up + archive daily
    # (05:15 / 05:30 PKT = just after the UTC day closes).
    for fn, cron, label in (
        (_reqlog("flush"), {"minute": "*"}, "every minute"),
        (_reqlog("rollup"), {"hour": 5, "minute": 15}, "daily 05:15"),
        (_reqlog("archive"), {"hour": 5, "minute": 30}, "daily 05:30"),
    ):
        sched.add_job(fn, trigger=CronTrigger(**cron, timezone=settings.timezone),
                      id=fn.__name__, name=fn.__name__)
        log.info("scheduled %-22s cron=%s", fn.__name__, label)

    # site search index (trade products, districts, plants, mandi, funds …): nightly rebuild
    sched.add_job(search_entities_refresh, trigger=CronTrigger(hour=4, minute=40, timezone=settings.timezone),
                  id="search_entities_refresh", name="search_entities_refresh")
    log.info("scheduled %-22s cron=daily 04:40", "search_entities_refresh")

    # GitHub Actions ingestion workflows, started on time (GitHub's own crons run hours late or not
    # at all; they stay as a backstop that skips itself after a dispatch — ingestion/gha_dispatch.py)
    from ingestion import gha_dispatch
    if gha_dispatch.configured():
        for i, d in enumerate(GHA_DISPATCH):
            sched.add_job(dispatch_workflow, trigger=CronTrigger(**d.cron, timezone=settings.timezone),
                          args=[d.workflow, d.inputs], id=f"gha:{d.workflow}#{i}", name=f"gha:{d.workflow}")
            log.info("scheduled %-22s cron=%s inputs=%s", f"gha:{d.workflow}", d.cron, d.inputs)
    else:
        log.info("GitHub Actions dispatch off (no GHA_DISPATCH_TOKEN / REQLOG_ARCHIVE_TOKEN) — "
                 "the workflows' own crons run them")

    # Website + API health (scripts/edge_health.py): uptime checks every 5 minutes, yesterday's Cloudflare
    # figures (UTC day, complete by 05:00 PKT) at 06:30, and the one daily health email at 07:00.
    for fn, cron, label in (
        (edge_probe_job, {"minute": "*/5"}, "every 5 min"),
        (edge_daily_job, {"hour": 6, "minute": 30}, "daily 06:30"),
        (health_email_job, {"hour": 7, "minute": 0}, "daily 07:00"),
    ):
        sched.add_job(fn, trigger=CronTrigger(**cron, timezone=settings.timezone), id=fn.__name__, name=fn.__name__)
        log.info("scheduled %-22s cron=%s", fn.__name__, label)

    # encrypted off-site copy of the night's verified dump (backup runs 20:00 UTC = 01:00 PKT)
    sched.add_job(offsite_backup_job, trigger=CronTrigger(hour=2, minute=45, timezone=settings.timezone),
                  id="offsite_backup", name="offsite_backup")
    log.info("scheduled %-22s cron=daily 02:45", "offsite_backup")
    return sched


def dispatch_workflow(workflow: str, inputs: dict) -> None:
    from ingestion import gha_dispatch
    try:
        gha_dispatch.run(workflow, inputs, redis_client=get_redis())
    except Exception:  # noqa: BLE001 — must not kill the scheduler
        log.exception("dispatch of %s raised", workflow)


def edge_probe_job() -> None:
    from scripts import edge_health
    try:
        edge_health.probe(get_redis())
    except Exception:  # noqa: BLE001 — must not kill the scheduler; the next run is 5 min away
        log.exception("edge probe failed")


def edge_daily_job() -> None:
    from scripts import edge_health
    try:
        edge_health.daily()
    except Exception as exc:  # noqa: BLE001
        log.exception("edge daily failed")
        alerting.alert("edge_health.daily failed", str(exc)[:500])


def health_email_job() -> None:
    from scripts import edge_health
    try:
        edge_health.daily_email(get_redis())
    except Exception as exc:  # noqa: BLE001
        log.exception("daily health email failed")
        alerting.alert("daily health email failed", str(exc)[:500])


def offsite_backup_job() -> None:
    from ingestion import offsite_backup
    try:
        offsite_backup.run(redis_client=get_redis())
    except Exception as exc:  # noqa: BLE001 — must not kill the scheduler
        log.exception("offsite_backup failed")
        alerting.alert("off-site backup failed", str(exc)[:500])


def search_entities_refresh() -> None:
    from ingestion import search_entities
    try:
        search_entities.refresh()
    except Exception as exc:  # noqa: BLE001
        log.exception("search_entities.refresh failed")
        alerting.alert("search_entities.refresh failed", str(exc)[:500])


def _reqlog(action: str):
    """A scheduler entry for ingestion.request_log.<action> that never raises."""
    def run() -> None:
        from ingestion import request_log
        try:
            getattr(request_log, action)()
        except Exception as exc:  # noqa: BLE001 — telemetry housekeeping must not kill the scheduler
            log.exception("request_log.%s failed", action)
            if action != "flush":  # flush retries every minute; a failed daily step is worth a mail
                alerting.alert(f"request_log.{action} failed", str(exc)[:500])
    run.__name__ = f"request_log_{action}"
    return run


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Prevent httpx from logging full request URLs (would leak api_key params).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    log.info("PakData scheduler starting on %s (tz=%s)", socket.gethostname(), settings.timezone)
    sched = build_scheduler()
    log.info("resolved schedule: %d job triggers + staleness_check", len(SCHEDULES))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler stopping")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
