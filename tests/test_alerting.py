"""alert_unless_403 suppresses the email for transient MUFAP Cloudflare 403s.

A persistent 403 is re-run every ~20 min by the autoheal watchdog; without this
guard each retry emailed once → dozens of alert emails for one outage. The 403 is
logged only; every other failure still alerts. Regression for the MUFAP fund jobs
(navs/returns/stats/portfolio) whose overridden run() bypassed the base 403 guard.
"""
from __future__ import annotations

from ingestion import alerting


def test_403_message_is_suppressed(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alerting, "alert", lambda title, msg: calls.append((title, msg)))
    alerting.alert_unless_403("mufap_fund_navs: job failed", "HTTP Error 403: ")
    assert calls == []  # no delivery for a 403


def test_non_403_failure_still_alerts(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alerting, "alert", lambda title, msg: calls.append((title, msg)))
    alerting.alert_unless_403("mufap_fund_navs: job failed", "HTTP Error 500: boom")
    assert calls == [("mufap_fund_navs: job failed", "HTTP Error 500: boom")]


def test_403_anywhere_in_message_is_suppressed(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(alerting, "alert", lambda title, msg: calls.append((title, msg)))
    alerting.alert_unless_403("x: fund fetch failed", "fund 12843: HTTP Error 403: Forbidden")
    assert calls == []
