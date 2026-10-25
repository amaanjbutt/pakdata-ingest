from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import ingestion.scheduler as sched
from ingestion.schedule import (
    MONITORED_EXTERNAL,
    SCHEDULES,
    expected_intervals,
)


# ---- schedule declarations ---------------------------------------------------

def test_expected_intervals_takes_tightest():
    ivals = expected_intervals()
    # pbs_spi_weekly is declared twice (Fri + Sat); both 9 days.
    assert ivals["pbs_spi_weekly"] == timedelta(days=9)
    # easydata_sync is monitored (runs on GitHub Actions) with a 3-day interval.
    assert ivals["easydata_sync"] == timedelta(days=3)


def test_every_schedule_job_has_valid_cron_keys():
    valid = {"year", "month", "day", "week", "day_of_week", "hour", "minute", "second"}
    for s in SCHEDULES:
        assert set(s.cron).issubset(valid), s.job


# ---- staleness detection -----------------------------------------------------

@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    """Keep the stale-alert throttle off the real Redis (state would leak between runs)."""
    fake = _FakeRedis()
    monkeypatch.setattr(sched, "get_redis", lambda: fake)
    return fake

def _patch_time(monkeypatch, now):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(sched, "datetime", _DT)


def test_staleness_flags_stale_registered_job(monkeypatch):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _patch_time(monkeypatch, now)
    # easydata_sync last succeeded 5 days ago; expected every 3 days -> stale.
    monkeypatch.setattr(sched, "_last_success_at",
                        lambda: {"easydata_sync": now - timedelta(days=5)})
    monkeypatch.setattr(sched, "_last_run_rows", lambda: {})
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(sched.alerting, "alert",
                        lambda t, m: alerts.append((t, m)))
    stale = sched.staleness_check()
    assert "easydata_sync" in stale
    assert any("easydata_sync" in t for t, _ in alerts)


def test_staleness_fresh_job_not_flagged(monkeypatch):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _patch_time(monkeypatch, now)
    fresh = {j: now for j in expected_intervals()}
    monkeypatch.setattr(sched, "_last_success_at", lambda: fresh)
    monkeypatch.setattr(sched, "_last_run_rows",
                        lambda: {j: {"started_at": now} for j in fresh})
    monkeypatch.setattr(sched.alerting, "alert",
                        lambda t, m: (_ for _ in ()).throw(AssertionError("should not alert")))
    assert sched.staleness_check() == []


def test_staleness_ignores_unregistered_jobs(monkeypatch):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _patch_time(monkeypatch, now)
    monkeypatch.setattr(sched, "_last_success_at", lambda: {})
    monkeypatch.setattr(sched, "_last_run_rows", lambda: {})
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: None)
    stale = sched.staleness_check()
    # sbp_policy_rate/mufap_pkrv/etc are declared but not in JOBS yet.
    from ingestion.registry import JOBS
    for name in stale:
        assert name in JOBS
    # every registered scheduled job with no success on record should be stale
    registered = {s.job for s in [*SCHEDULES, *MONITORED_EXTERNAL]} & set(JOBS)
    assert set(stale) == registered


def test_staleness_alert_throttled_then_rearmed(monkeypatch):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _patch_time(monkeypatch, now)
    last = {"easydata_sync": now - timedelta(days=5)}
    monkeypatch.setattr(sched, "_last_success_at", lambda: last)
    monkeypatch.setattr(sched, "_last_run_rows", lambda: {})
    alerts: list[str] = []
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: alerts.append(t))

    sched.staleness_check()
    first = [t for t in alerts if "easydata_sync" in t]
    assert len(first) == 1
    sched.staleness_check()  # an hour later, still stale -> throttled
    assert len([t for t in alerts if "easydata_sync" in t]) == 1

    last["easydata_sync"] = now  # recovers -> throttle cleared
    sched.staleness_check()
    last["easydata_sync"] = now - timedelta(days=5)  # lapses again -> alerts at once
    sched.staleness_check()
    assert len([t for t in alerts if "easydata_sync" in t]) == 2


def test_staleness_alert_fails_open_without_redis(monkeypatch):
    now = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
    _patch_time(monkeypatch, now)
    monkeypatch.setattr(sched, "_last_success_at",
                        lambda: {"easydata_sync": now - timedelta(days=5)})
    monkeypatch.setattr(sched, "_last_run_rows", lambda: {})

    def _boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(sched, "get_redis", _boom)
    alerts: list[str] = []
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: alerts.append(t))
    sched.staleness_check()
    assert any("easydata_sync" in t for t in alerts)


# ---- stuck-run reaping -------------------------------------------------------

def test_stuck_run_check_reaps_and_alerts(monkeypatch):
    rows = [
        {"id": 14, "job_name": "mufap_pkrv", "started_at": datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)},
        {"id": 21, "job_name": "easydata_sync", "started_at": datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)},
    ]
    monkeypatch.setattr(sched.db, "query", lambda *a, **k: rows)
    updates = []
    monkeypatch.setattr(sched.db, "execute", lambda sql, params: updates.append(params))
    alerts = []
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: alerts.append(t))
    reaped = sched.stuck_run_check()
    assert reaped == [14, 21]
    assert len(updates) == 2
    # the run id is the last bound param of the UPDATE
    assert [u[-1] for u in updates] == [14, 21]
    assert any("mufap_pkrv" in a for a in alerts)


def test_stuck_run_check_noop_when_clean(monkeypatch):
    monkeypatch.setattr(sched.db, "query", lambda *a, **k: [])
    monkeypatch.setattr(sched.db, "execute",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no update expected")))
    monkeypatch.setattr(sched.alerting, "alert",
                        lambda t, m: (_ for _ in ()).throw(AssertionError("no alert expected")))
    assert sched.stuck_run_check() == []


# ---- cross-process lock ------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    def delete(self, k):
        return 1 if self.store.pop(k, None) is not None else 0

    def eval(self, script, numkeys, key, arg):
        if self.store.get(key) == arg:
            del self.store[key]
            return 1
        return 0


def test_lock_prevents_double_acquire(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sched, "get_redis", lambda: fake)
    t1 = sched._acquire("sbp_kibor")
    assert t1 is not None
    assert sched._acquire("sbp_kibor") is None  # held
    sched._release("sbp_kibor", t1)
    assert sched._acquire("sbp_kibor") is not None  # freed


def test_run_job_skips_when_locked(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(sched, "get_redis", lambda: fake)
    ran = []

    class _Job:
        name = "sbp_kibor"
        def run(self):
            ran.append(1)
            return {"status": "success"}

    monkeypatch.setitem(sched.JOBS, "sbp_kibor", _Job)
    fake.set(sched._lock_key("sbp_kibor"), "someoneelse")  # pre-held
    sched.run_job("sbp_kibor")
    assert ran == []  # skipped because lock held
