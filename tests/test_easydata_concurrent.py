"""_process_series_concurrent: parallel fetch, serialized quota, correct aggregation.

Network/DB are mocked; a FakeQuota enforces a call budget. Verifies: all series
processed under ample budget; clean 'partial' + stop when budget runs out; a 429
rotates and still succeeds; a non-429 error skips just that series. Thread-safety is
exercised by running 4 workers over many series.
"""
from __future__ import annotations

import threading

from ingestion.easydata_config import EasyDataSeries
from ingestion.jobs import easydata_sync as eds


def _series(n: int) -> list[EasyDataSeries]:
    return [
        EasyDataSeries(
            easydata_key=f"K{i}", id=f"s.{i}", module="forex", name=f"n{i}",
            description="", unit="", frequency="monthly", source="SBP", tier="basic",
            min_value=None, max_value=None, max_step=None, backfill_start="2000-01-01",
            easydata_dataset_code="DS", easydata_last_refresh=None,
        )
        for i in range(n)
    ]


class FakeQuota:
    """Serialized by the method's lock, so no internal locking needed here."""
    def __init__(self, budget: int):
        self.budget = budget
        self.calls = 0
        self.api_key = "k1"
    def allow(self) -> bool:
        return self.calls < self.budget
    def record(self) -> None:
        self.calls += 1
    def penalize(self) -> None:
        pass


def _job(monkeypatch, *, raise_429_on=frozenset(), raise_err_on=frozenset()):
    job = eds.EasyDataSyncJob.__new__(eds.EasyDataSyncJob)  # skip __init__ (no env/keys)
    job.api_keys = ["k1", "k2", "k3"]
    job.api_key = "k1"
    seen_429: set[str] = set()

    def fake_get(url: str):
        # url carries the series id via _data_url; we stub _data_url to return the id
        sid = url
        if sid in raise_err_on:
            raise ValueError("boom 500")
        if sid in raise_429_on and sid not in seen_429:
            seen_429.add(sid)
            raise RuntimeError("HTTP Error 429: ")
        return b'{"columns":[],"rows":[]}'  # IngestionJob.http_get returns bytes (.content)

    monkeypatch.setattr(job, "_data_url", lambda s, backfill, key: s.id)
    monkeypatch.setattr(job, "http_get", fake_get)
    monkeypatch.setattr(job, "upsert", lambda v: 1)
    monkeypatch.setattr(job, "validate", lambda recs: recs)
    monkeypatch.setattr(job, "_update_refresh", lambda sid, r: None)
    monkeypatch.setattr(eds, "parse_easydata_json", lambda text: [object()])
    monkeypatch.setattr(eds.storage, "archive", lambda *a, **k: "/tmp/x")
    return job


def test_all_processed_with_ample_budget(monkeypatch):
    job = _job(monkeypatch)
    status, rows, processed = job._process_series_concurrent(_series(20), FakeQuota(1000), False, {}, 4)
    assert processed == 20 and rows == 20 and status == "success"


def test_stops_partial_when_budget_exhausted(monkeypatch):
    job = _job(monkeypatch)
    status, rows, processed = job._process_series_concurrent(_series(50), FakeQuota(10), False, {}, 4)
    assert processed == 10 and rows == 10 and status == "partial"


def test_429_rotates_and_succeeds(monkeypatch):
    job = _job(monkeypatch, raise_429_on={"s.3", "s.7"})
    status, rows, processed = job._process_series_concurrent(_series(10), FakeQuota(1000), False, {}, 4)
    assert processed == 10 and status == "success"  # rotated past the 429s


def test_non_429_error_skips_only_that_series(monkeypatch):
    job = _job(monkeypatch, raise_err_on={"s.5"})
    status, rows, processed = job._process_series_concurrent(_series(10), FakeQuota(1000), False, {}, 4)
    assert processed == 9 and rows == 9  # the one bad series skipped, rest fine
