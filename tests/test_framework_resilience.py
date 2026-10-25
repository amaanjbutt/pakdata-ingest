"""A multi-file backfill must not be lost to one malformed file."""
from __future__ import annotations

from datetime import date

import pytest

from ingestion.framework import FetchedFile, IngestionJob, Record


class _Job(IngestionJob):
    name = "test_job"
    source = "TEST"

    def __init__(self, files, bad: set[str]):
        self._files = files
        self._bad = bad

    def fetch(self, backfill: bool = False):
        return self._files

    def parse(self, f: FetchedFile):
        if f.filename in self._bad:
            raise ValueError(f"malformed: {f.filename}")
        return [Record("s.one", f.when, 1.0, {})]


def _files(n: int):
    return [
        FetchedFile(filename=f"f{i}.csv", content=b"x", when=date(2026, 1, i + 1))
        for i in range(n)
    ]


def _wire(job, monkeypatch, captured):
    import ingestion.framework as fw

    monkeypatch.setattr(job, "_start_run", lambda: 1)
    monkeypatch.setattr(job, "_finish",
                        lambda rid, status, rows, err, raw: captured.update(
                            status=status, rows=rows, error=err))
    monkeypatch.setattr(job, "validate", lambda recs: [(r, False) for r in recs])
    monkeypatch.setattr(job, "upsert", lambda v: len(v))
    monkeypatch.setattr(fw.storage, "archive", lambda *a, **k: "/tmp/x")
    monkeypatch.setattr(fw.alerting, "alert", lambda *a, **k: None)


def test_one_bad_file_does_not_lose_the_backfill(monkeypatch):
    captured = {}
    job = _Job(_files(5), bad={"f2.csv"})
    _wire(job, monkeypatch, captured)
    result = job.run(backfill=True)
    # 4 of 5 files still ingested; run reported as partial, not failed
    assert result["status"] == "partial"
    assert result["rows"] == 4
    assert result["skipped"] == 1
    assert captured["status"] == "partial"


def test_all_files_bad_raises(monkeypatch):
    captured = {}
    job = _Job(_files(3), bad={"f0.csv", "f1.csv", "f2.csv"})
    _wire(job, monkeypatch, captured)
    with pytest.raises(Exception):
        job.run(backfill=True)


def test_out_of_bounds_record_dropped_not_whole_run(monkeypatch):
    """One bad cell in a multi-year backfill must not destroy the run."""
    import ingestion.framework as fw

    job = _Job([], bad=set())
    monkeypatch.setattr(job, "_catalog_bounds", lambda ids: {
        "rates.pkrv.10y": {"min_value": 0, "max_value": 40, "max_step": None}})
    monkeypatch.setattr(job, "_prev_value", lambda sid, dims: None)
    monkeypatch.setattr(fw.alerting, "alert", lambda *a, **k: None)
    recs = [
        Record("rates.pkrv.10y", date(2024, 3, 1), 12.5, {}),
        Record("rates.pkrv.10y", date(2024, 3, 4), 42.0, {}),   # source typo
        Record("rates.pkrv.10y", date(2024, 3, 5), 12.7, {}),
    ]
    out = job.validate(recs)
    assert [r.value for r, _ in out] == [12.5, 12.7]  # good ones survive
    assert len(job.last_rejects) == 1
    assert "42.0" in job.last_rejects[0]


def test_all_records_rejected_raises(monkeypatch):
    import ingestion.framework as fw

    job = _Job([], bad=set())
    monkeypatch.setattr(job, "_catalog_bounds", lambda ids: {
        "rates.pkrv.10y": {"min_value": 0, "max_value": 40, "max_step": None}})
    monkeypatch.setattr(fw.alerting, "alert", lambda *a, **k: None)
    with pytest.raises(fw.ValidationError):
        job.validate([Record("rates.pkrv.10y", date(2024, 3, 4), 99.0, {})])


def test_unknown_series_dropped_not_fatal(monkeypatch):
    import ingestion.framework as fw

    job = _Job([], bad=set())
    monkeypatch.setattr(job, "_catalog_bounds", lambda ids: {
        "known.x": {"min_value": None, "max_value": None, "max_step": None}})
    monkeypatch.setattr(job, "_prev_value", lambda sid, dims: None)
    monkeypatch.setattr(fw.alerting, "alert", lambda *a, **k: None)
    out = job.validate([
        Record("known.x", date(2024, 1, 1), 1.0, {}),
        Record("not.in.catalog", date(2024, 1, 1), 2.0, {}),
    ])
    assert [r.series_id for r, _ in out] == ["known.x"]


def test_clean_run_is_success(monkeypatch):
    captured = {}
    job = _Job(_files(3), bad=set())
    _wire(job, monkeypatch, captured)
    result = job.run()
    assert result["status"] == "success"
    assert result["rows"] == 3


def test_dedupe_last_keeps_one_value_per_point_newest_last():
    """Two files restating one point must not write it twice in a run (each flip would
    be logged as a revision); the last-parsed (newest report) value wins."""
    from datetime import date

    from ingestion.framework import Record, dedupe_last

    recs = [Record("a", date(2025, 9, 30), 54483984.0, {}),   # Q1 review, preliminary
            Record("b", date(2025, 9, 30), 1.0, {"city": "x"}),
            Record("a", date(2025, 9, 30), 54796316.0, {})]   # Q3 review, revised
    out = dedupe_last(recs)
    assert [(r.series_id, r.value) for r in out] == [("a", 54796316.0), ("b", 1.0)]
