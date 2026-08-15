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
import random
import socket
import uuid
from datetime import datetime, timedelta, timezone

import redis
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import settings
from ingestion import alerting
from ingestion.registry import JOBS
from ingestion.schedule import SCHEDULES, expected_intervals, stale_on_run_jobs

log = logging.getLogger("pakdata.scheduler")

MAX_JITTER_SECONDS = 120
LOCK_TTL_SECONDS = int(os.getenv("JOB_LOCK_TTL", "7200"))  # 2h; covers a slow run
STALENESS_CHECK_HOURS = 1
# A run still 'running' after this long is presumed dead (killed process, crashed
# container). Long backfills legitimately run for hours, so keep this generous.
STUCK_RUN_HOURS = int(os.getenv("STUCK_RUN_HOURS", "12"))

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
    rows = db.query(
        """
        SELECT DISTINCT ON (job_name) job_name, started_at
        FROM ingestion_runs
        WHERE status IN ('success', 'no_new_data', 'partial')
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


def staleness_check() -> list[str]:
    """Alert on jobs whose freshness has lapsed. Returns the list of stale job
    names (also useful for tests). Only checks jobs that are actually
    registered — declared-but-unbuilt jobs are ignored."""
    intervals = expected_intervals()
    stale_on_run = stale_on_run_jobs()
    last_success = _last_success_at()
    last_run = _last_run_rows()
    now = datetime.now(timezone.utc)
    stale: list[str] = []

    for job_name, interval in intervals.items():
        if job_name not in JOBS:
            continue  # declared ahead of implementation
        # For "changes rarely" jobs, freshness = last run attempt, not last data.
        ref = last_run.get(job_name, {}).get("started_at") if job_name in stale_on_run \
            else last_success.get(job_name)
        if ref is None:
            stale.append(job_name)
            alerting.alert(
                f"staleness: {job_name} has never succeeded",
                f"No successful run on record; expected every {interval}.",
            )
            continue
        age = now - ref
        if age > interval:
            stale.append(job_name)
            alerting.alert(
                f"staleness: {job_name} is stale",
                f"Last fresh run {age} ago (> expected {interval}).",
            )
    if not stale:
        log.info("staleness check: all %d registered scheduled jobs fresh", len(intervals))
    return stale


def health_check() -> dict:
    """Hourly health sweep. Reaps wedged runs *first* so that a dead job stops
    masquerading as in-flight, then evaluates staleness against real statuses."""
    reaped = stuck_run_check()
    stale = staleness_check()
    return {"reaped": reaped, "stale": stale}


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
    log.info("scheduled %-22s cron=hourly (stuck-run reap + staleness)", "health_check")
    return sched


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
