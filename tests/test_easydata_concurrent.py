"""_process_series_concurrent: parallel fetch, serialized quota, correct aggregation.

Network/DB are mocked; a FakeQuota enforces a call budget. Verifies: all series
processed under ample budget; clean 'partial' + stop when budget runs out; a 429
rotates and still succeeds; a non-429 error skips just that series. Thread-safety is
exercised by running 4 workers over many series.
"""
from __future__ import annotations


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


def test_db_state_overlays_refresh_date_and_module(monkeypatch):
    """The static config's refresh date and module must not drive selection: the DB's
    later refresh date wins (else advanced datasets are re-pulled forever) and the DB
    module sets the pull priority."""
    from ingestion.easydata_config import EasyDataSeries
    from ingestion.jobs import easydata_sync as es

    def mk(key, sid, ref):
        return EasyDataSeries(easydata_key=key, id=sid, module="economic", name=sid, description="",
                              unit="", frequency="monthly", source="SBP", tier="basic", min_value=None,
                              max_value=None, max_step=None, backfill_start="2000-01-01",
                              easydata_dataset_code="TS_X", easydata_last_refresh=ref)

    cfg = [mk("TS_X.A", "social.a", "2026-07-27"), mk("TS_X.B", "x.b", "2026-09-01"), mk("TS_X.C", "x.c", None)]
    monkeypatch.setattr(es.db, "query", lambda sql, params=None: [
        {"id": "social.a", "module": "social", "easydata_last_refresh": "2026-09-10"},
        {"id": "x.b", "module": "external", "easydata_last_refresh": "2026-08-01"},  # config is later
    ])
    out = {s.id: s for s in es.EasyDataSyncJob._with_db_state(cfg)}
    assert (out["social.a"].easydata_last_refresh, out["social.a"].module) == ("2026-09-10", "social")
    assert (out["x.b"].easydata_last_refresh, out["x.b"].module) == ("2026-09-01", "external")
    assert out["x.c"] == cfg[2]  # not in the DB: unchanged
    assert es._module_priority(out["social.a"]) < es._module_priority(cfg[0])


class FakePool:
    """QuotaPool's selection rule (first key not retired) with a named-key retire."""
    def __init__(self, keys: list[str]):
        self.keys, self.blocked, self.calls = list(keys), set(), 0
        self.api_key = keys[0]
    def allow(self) -> bool:
        for k in self.keys:
            if k not in self.blocked:
                self.api_key = k
                return True
        return False
    def record(self) -> None:
        self.calls += 1
    def penalize(self, seconds: float = 3600.0) -> None:
        self.blocked.add(self.api_key)
    def penalize_key(self, key: str, seconds: float = 3600.0) -> None:
        self.blocked.add(key)


def _keyed_job(monkeypatch, keys, refuse):
    """A job whose fake EasyData answers 401 when refuse(series_id, key) is true."""
    job = eds.EasyDataSyncJob.__new__(eds.EasyDataSyncJob)
    job.api_keys, job.api_key, job._alerted_401 = list(keys), keys[0], False
    job._dead_keys = set()

    def fake_get(url: str):
        sid, key = url.split("|")
        if refuse(sid, key):
            raise RuntimeError("HTTP Error 401: ")
        return b'{"columns":[],"rows":[]}'

    monkeypatch.setattr(job, "_data_url", lambda s, backfill, key: f"{s.id}|{key}")
    monkeypatch.setattr(job, "http_get", fake_get)
    monkeypatch.setattr(job, "upsert", lambda v: 1)
    monkeypatch.setattr(job, "validate", lambda recs: recs)
    monkeypatch.setattr(job, "_update_refresh", lambda sid, r: None)
    monkeypatch.setattr(eds, "parse_easydata_json", lambda text: [object()])
    monkeypatch.setattr(eds.storage, "archive", lambda *a, **k: "/tmp/x")
    monkeypatch.setattr(eds.alerting, "alert", lambda *a, **k: None)
    return job


def test_dead_key_is_retired_after_two_401s(monkeypatch):
    """The 2026-11 runs: one key in the pool answered 401 and was handed out until its
    hourly budget ran out (225 series skipped every run, status `success`). Now it costs
    two series (retried next run) and the run names it."""
    job = _keyed_job(monkeypatch, ["dead", "live"], lambda sid, key: key == "dead")
    pool = FakePool(["dead", "live"])
    status, rows, processed = job._process_series_concurrent(_series(20), pool, False, {}, 1)
    assert processed == 18 and rows == 18 and status == "success"
    assert pool.blocked == {"dead"} and job._dead_keys == {1}
    note = eds.unauthorized_note(sorted(job._dead_keys), len(job.api_keys))
    assert "#1 of 2" in note and "401" in note and "dead" not in note  # position, never the key


def test_one_refused_series_does_not_retire_a_good_key(monkeypatch):
    job = _keyed_job(monkeypatch, ["k1", "k2"], lambda sid, key: sid == "s.3")
    pool = FakePool(["k1", "k2"])
    status, rows, processed = job._process_series_concurrent(_series(10), pool, False, {}, 1)
    assert processed == 9 and not pool.blocked and not job._dead_keys


def test_unauthorized_note_is_none_without_dead_keys():
    assert eds.unauthorized_note([], 3) is None
    assert "#2, #3 of 3" in eds.unauthorized_note([3, 2], 3)
