"""Unit tests for the EasyData key-expiry warning + 401 detection.

Pure-logic tests (no DB, no network): the expiry check reads a date env var and
routes to alerting; the 401 helper classifies an exception string.
"""
from __future__ import annotations

from datetime import date, timedelta


def test_is_unauthorized_classifies_401():
    from ingestion.jobs.easydata_sync import _is_unauthorized

    assert _is_unauthorized(Exception("HTTP Error 401: Unauthorized"))
    assert not _is_unauthorized(Exception("HTTP Error 429: Too Many Requests"))
    assert not _is_unauthorized(Exception("connection reset"))


def _iso(days_from_today: int) -> str:
    return (date.today() + timedelta(days=days_from_today)).isoformat()


def test_expiry_warns_within_threshold(monkeypatch):
    from ingestion import scheduler

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler.alerting, "alert", lambda t, m: calls.append((t, m)))
    monkeypatch.setenv("EASYDATA_KEY_EXPIRIES", f"orig:{_iso(7)}")
    assert scheduler.key_expiry_check() == ["orig"]
    assert calls and "expiring in 7d" in calls[0][0]


def test_expiry_alerts_when_expired(monkeypatch):
    from ingestion import scheduler

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler.alerting, "alert", lambda t, m: calls.append((t, m)))
    monkeypatch.setenv("EASYDATA_KEY_EXPIRIES", f"k1:{_iso(-2)}")
    assert scheduler.key_expiry_check() == ["k1"]
    assert "EXPIRED" in calls[0][0]


def test_expiry_quiet_when_far_or_between_thresholds(monkeypatch):
    from ingestion import scheduler

    def boom(*_a):
        raise AssertionError("should not alert")

    monkeypatch.setattr(scheduler.alerting, "alert", boom)
    # 100 days out, and 10 days out (not a threshold day) → no alert
    monkeypatch.setenv("EASYDATA_KEY_EXPIRIES", f"a:{_iso(100)},b:{_iso(10)}")
    assert scheduler.key_expiry_check() == []


def test_expiry_unset_is_noop(monkeypatch):
    from ingestion import scheduler

    monkeypatch.delenv("EASYDATA_KEY_EXPIRIES", raising=False)
    assert scheduler.key_expiry_check() == []


def test_load_api_keys_merges_list_and_single_secret(monkeypatch):
    from ingestion.jobs.easydata_sync import _load_api_keys

    monkeypatch.setenv("EASYDATA_API_KEYS", "k1, k2,k3")
    monkeypatch.setenv("EASYDATA_API_KEY", "k4")
    assert _load_api_keys() == ["k1", "k2", "k3", "k4"]
    monkeypatch.setenv("EASYDATA_API_KEY", "k2")          # duplicate dropped
    assert _load_api_keys() == ["k1", "k2", "k3"]
    monkeypatch.delenv("EASYDATA_API_KEYS")
    assert _load_api_keys() == ["k2"]


def test_expired_key_is_retired_and_the_call_rotates():
    from ingestion.jobs.easydata_sync import EasyDataSyncJob

    class Pool:
        def __init__(self):
            self.keys, self.i, self.blocked = ["dead", "live"], 0, {}
        @property
        def api_key(self):
            return self.keys[self.i]
        def record(self):
            pass
        def penalize(self, seconds=3600.0):
            self.blocked[self.keys[self.i]] = seconds
            self.i += 1
        def allow(self):
            return self.i < len(self.keys)

    job = EasyDataSyncJob.__new__(EasyDataSyncJob)
    job.api_keys, job.api_key, job._alerted_401 = ["dead", "live"], "dead", True

    def get(url):
        if "dead" in url:
            raise Exception("HTTP Error 401: Unauthorized")
        return b"ok"

    job.http_get = get
    pool = Pool()
    assert job._http_get_rotating(pool, lambda k: f"https://x/?api_key={k}") == b"ok"
    assert pool.blocked == {"dead": 30 * 86400.0}
