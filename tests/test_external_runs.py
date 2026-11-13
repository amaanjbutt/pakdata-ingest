"""ingestion/external_runs.py: GitHub Actions runs can't email, so the VPS reads their rows.

The rows below are the real error texts from prod's ingestion_runs (2026-10/11)."""
from __future__ import annotations

from ingestion import external_runs as er
from ingestion.jobs.easydata_sync import unauthorized_note


def ok(n=1):
    return [{"status": "success", "error": None}] * n


def test_rejected_key_alerts_even_on_a_successful_run():
    note = unauthorized_note([2], 3)
    msg = er.problem([{"status": "success", "error": note}] + ok(2))
    assert msg and "#2 of 3" in msg


def test_one_failure_is_a_blip_two_are_not():
    sbp_503 = {"status": "failed", "error": "HTTP Error 503: "}
    assert er.problem([sbp_503] + ok(2)) is None                      # 2026-11-02: healed next run
    msg = er.problem([sbp_503, {"status": "failed", "error": "HTTP Error 404: "}, ok()[0]])
    assert msg and "503" in msg


def test_mufap_403s_stay_with_the_staleness_check():
    f403 = {"status": "failed", "error": "payouts: HTTP Error 403: "}
    assert er.problem([f403, f403]) is None


def test_running_rows_and_clean_history_are_quiet():
    assert er.problem([{"status": "running", "error": None}] + ok(2)) is None
    assert er.problem([]) is None
    assert er.problem([{"status": "partial", "error": "run budget reached; 401/638 funds"}]) is None
    assert er.problem([{"status": "success", "error": "all 3 record(s) rejected; first: x"}]) is None


class FakeRedis:
    def __init__(self):
        self.keys = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.keys:
            return False
        self.keys[k] = v
        return True

    def delete(self, k):
        self.keys.pop(k, None)


def test_alerts_once_per_day_and_rearms(monkeypatch):
    sent = []
    monkeypatch.setattr(er.alerting, "alert", lambda t, m: sent.append(t))
    runs = {"easydata_sync": [{"status": "success", "error": unauthorized_note([1], 3)}],
            "sbp_kibor": ok(2)}
    monkeypatch.setattr(er, "_recent_runs", lambda jobs: runs)
    r = FakeRedis()
    assert set(er.external_run_check(r)) == {"easydata_sync"}
    er.external_run_check(r)
    assert sent == ["easydata_sync: GitHub Actions run problem"]       # throttled
    runs["easydata_sync"] = ok()
    er.external_run_check(r)                                           # clean → re-armed
    runs["easydata_sync"] = [{"status": "success", "error": unauthorized_note([1], 3)}]
    er.external_run_check(r)
    assert len(sent) == 2
