"""MUFAP IP-block resilience: pre-fetched page bundle, per-job slot skip, and the
resumable portfolio run (budget / 403-streak stop → partial, never stuck)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from ingestion import http_bundle, http_client
from ingestion.jobs import mufap_fund_portfolio as pf
from ingestion.jobs.mufap_fund_stats import PAYOUTS_WINDOW_DAYS, payouts_url
from ingestion.jobs.mufap_fund_navs import MufapFundNavsJob
from scripts.mufap_fetch_bundle import bundle_urls
from scripts.mufap_jobs_todo import DAILY_JOBS, jobs_todo


def test_bundle_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv(http_bundle.BUNDLE_ENV, str(tmp_path))
    http_bundle.save(tmp_path, "https://x.test/a?tab=1", b"<html>a</html>")
    http_bundle.save(tmp_path, "https://x.test/b?tab=3", b"<html>b</html>")
    assert http_bundle.lookup("https://x.test/a?tab=1") == b"<html>a</html>"
    assert http_bundle.lookup("https://x.test/b?tab=3") == b"<html>b</html>"
    assert http_bundle.lookup("https://x.test/missing") is None


def test_bundle_off_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv(http_bundle.BUNDLE_ENV, raising=False)
    http_bundle.save(tmp_path, "https://x.test/a", b"x")
    assert http_bundle.lookup("https://x.test/a") is None


def test_framework_http_get_prefers_bundle(tmp_path, monkeypatch):
    url = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=3"
    http_bundle.save(tmp_path, url, b"bundled")
    monkeypatch.setenv(http_bundle.BUNDLE_ENV, str(tmp_path))

    def no_network(*a, **k):
        raise AssertionError("network used despite bundle")

    monkeypatch.setattr(http_client, "get", no_network)
    assert MufapFundNavsJob().http_get(url) == b"bundled"


def test_framework_http_get_falls_through_when_not_bundled(tmp_path, monkeypatch):
    monkeypatch.setenv(http_bundle.BUNDLE_ENV, str(tmp_path))

    class R:
        content = b"live"

    monkeypatch.setattr(http_client, "get", lambda url, timeout=30: R())
    assert MufapFundNavsJob().http_get("https://x.test/live") == b"live"


def test_bundle_payouts_url_matches_stats_job():
    today = date(2026, 10, 4)
    want = payouts_url(today - timedelta(days=PAYOUTS_WINDOW_DAYS), today)
    urls = bundle_urls(today)
    assert want in urls
    assert urls[0].endswith("tab=1")  # the returns page doubles as the IP probe


def test_jobs_todo_keeps_order_and_skips_done():
    assert jobs_todo(set()) == DAILY_JOBS
    assert jobs_todo({"mufap_fund_navs", "mufap_pkrv"}) == [
        "mufap_fund_returns", "mufap_fund_stats",
        "mufap_debt_prices", "mufap_tfc_valuations", "mufap_debt_trades",
    ]
    assert jobs_todo(set(DAILY_JOBS)) == []


# ---- resumable portfolio ------------------------------------------------------

class _Resp:
    text = "{}"


def _job(monkeypatch, fund_ids, post):
    job = pf.MufapFundPortfolioJob()
    finished = {}
    monkeypatch.setattr(job, "_start_run", lambda: 1)
    monkeypatch.setattr(job, "_finish", lambda rid, status, rows, err, raw:
                        finished.update(status=status, rows=rows, err=err))
    monkeypatch.setattr(job, "_funds_to_refresh", lambda everything=False: list(fund_ids))
    monkeypatch.setattr(job, "_upsert", lambda fid, as_of, rows: len(rows))
    monkeypatch.setattr(pf.http_client, "post", post)
    monkeypatch.setattr(pf, "parse_portfolio",
                        lambda text: (date(2026, 9, 1), [("Cash", 60.0), ("PIBs", 40.0)]))
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    monkeypatch.setattr(pf.alerting, "alert_unless_403", lambda *a, **k: None)
    return job, finished


def test_portfolio_stops_partial_on_403_streak(monkeypatch):
    calls = {"n": 0}

    def post(url, json=None, timeout=60):
        calls["n"] += 1
        if calls["n"] > 3:  # IP gets blocked after 3 good funds
            raise RuntimeError("HTTP Error 403: Forbidden")
        return _Resp()

    job, fin = _job(monkeypatch, range(1, 101), post)
    out = job.run()
    assert out["status"] == "partial" and out["funds"] == 3
    assert fin["status"] == "partial" and "403" in fin["err"]
    assert calls["n"] == 3 + pf.MAX_CONSECUTIVE_403  # stopped fast, not 100 calls


def test_portfolio_blocked_from_start_fails_as_403(monkeypatch):
    def post(url, json=None, timeout=60):
        raise RuntimeError("HTTP Error 403: Forbidden")

    job, fin = _job(monkeypatch, range(1, 50), post)
    with pytest.raises(RuntimeError, match="403"):
        job.run()
    assert fin["status"] == "failed"


def test_portfolio_budget_stop(monkeypatch):
    monkeypatch.setattr(pf, "RUN_BUDGET_SECONDS", -1)  # already past the deadline
    job, fin = _job(monkeypatch, [1, 2, 3], lambda *a, **k: _Resp())
    out = job.run()  # budget hit before any fund → partial, never stuck 'running'
    assert out["status"] == "partial" and out["funds"] == 0
    assert "budget" in fin["err"]
    job2, fin2 = _job(monkeypatch, [], lambda *a, **k: _Resp())
    assert job2.run()["status"] == "no_new_data"


def test_portfolio_full_queue_is_success(monkeypatch):
    job, fin = _job(monkeypatch, [1, 2, 3], lambda *a, **k: _Resp())
    out = job.run()
    assert out == {"status": "success", "rows": 6, "funds": 3, "queued": 3}
    assert fin["status"] == "success"
