from __future__ import annotations

from datetime import date, timedelta

import ingestion.jobs.easydata_sync as eds
from ingestion.easydata_config import EasyDataSeries
from ingestion.jobs.easydata_sync import EasyDataSyncJob


def _series(sid, freq="monthly"):
    return EasyDataSeries(
        easydata_key=sid.upper(), id=sid, module="economic", name=sid,
        description="", unit="", frequency=freq, source="SBP-EasyData",
        tier="basic", min_value=None, max_value=None, max_step=None,
        backfill_start="2000-01-01",
    )


def test_select_backfill_skips_fresh_series(monkeypatch):
    job = EasyDataSyncJob(quota_path=None)
    today = date.today()
    # a: fresh monthly (10d old) -> skip; b: stale monthly (200d) -> pull;
    # c: no data at all -> pull
    rows = [
        {"series_id": "a", "d": today - timedelta(days=10)},
        {"series_id": "b", "d": today - timedelta(days=200)},
    ]
    monkeypatch.setattr(eds.db, "query", lambda *a, **k: rows)
    todo = job._select_backfill([_series("a"), _series("b"), _series("c")])
    # 'a' is fresh (skipped); 'c' has no data (first); 'b' is stale-present (after).
    assert [s.id for s in todo] == ["c", "b"]


class _FakeQuota:
    """allow() returns the queued booleans; record() is counted."""

    def __init__(self, allows):
        self._allows = list(allows)
        self.records = 0

    def allow(self):
        return self._allows.pop(0) if self._allows else False

    def record(self):
        self.records += 1


def test_process_series_stops_partial_at_quota():
    job = EasyDataSyncJob(quota_path=None)
    calls: list[str] = []
    quota = _FakeQuota([False])  # budget already exhausted
    status, rows, processed = job._process_series(
        ["a", "b", "c"], quota, lambda s: (calls.append(s) or 5)
    )
    assert status == "partial"
    assert processed == 0
    assert rows == 0
    assert calls == []  # nothing fetched once the gate is closed


def test_process_series_success_path():
    job = EasyDataSyncJob(quota_path=None)
    quota = _FakeQuota([True, True])
    status, rows, processed = job._process_series(["a", "b"], quota, lambda s: 3)
    assert status == "success"
    assert processed == 2
    assert rows == 6


def test_process_series_stops_partial_midway():
    job = EasyDataSyncJob(quota_path=None)
    quota = _FakeQuota([True, False])  # first ok, then budget gone
    seen: list[str] = []
    status, rows, processed = job._process_series(
        ["a", "b", "c"], quota, lambda s: (seen.append(s) or 2)
    )
    assert status == "partial"
    assert processed == 1
    assert seen == ["a"]


def test_process_series_no_data():
    job = EasyDataSyncJob(quota_path=None)
    status, rows, processed = job._process_series([], _FakeQuota([]), lambda s: 0)
    assert status == "no_new_data"
    assert processed == 0


def test_process_series_skips_a_failing_series(caplog):
    """One series raising (source 500, bad code, DNS flap) must not abort the run:
    it is skipped and the rest still process."""
    job = EasyDataSyncJob(quota_path=None)
    quota = _FakeQuota([True, True, True])
    seen: list[str] = []

    def process_one(s):
        seen.append(s)
        if s == "b":
            raise RuntimeError("boom 500")
        return 4

    status, rows, processed = job._process_series(["a", "b", "c"], quota, process_one)
    assert seen == ["a", "b", "c"]      # did not stop at 'b'
    assert processed == 2               # a and c
    assert rows == 8
    assert status == "success"          # some succeeded -> not a failed run


def test_data_url_encodes_reserved_characters():
    job = EasyDataSyncJob(quota_path=None)
    job.api_key = "K"
    s = _series("x")
    object.__setattr__(s, "easydata_key", "TS_GP_BS_SNWALFR_Q.S&NWALR0010")
    url = job._data_url(s, backfill=True)
    assert "S%26NWALR0010" in url        # '&' percent-encoded, not a query break
    assert "S&NWALR0010" not in url
