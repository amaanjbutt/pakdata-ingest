"""The VPS clock for the GitHub Actions workflows (ingestion/gha_dispatch.py, schedule.GHA_DISPATCH)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml
from apscheduler.triggers.cron import CronTrigger

from ingestion import gha_dispatch as gd
from ingestion.schedule import GHA_DISPATCH

WORKFLOWS = Path(__file__).resolve().parents[2] / "ops" / "gha-ingest" / "workflows"
_DOW = {"0": "sun", "1": "mon", "2": "tue", "3": "wed", "4": "thu", "5": "fri", "6": "sat", "7": "sun"}


def _crontab_trigger(expr: str) -> CronTrigger:
    """A GitHub (crontab) expression as an APScheduler trigger in UTC. APScheduler numbers weekdays from
    Monday = 0, crontab from Sunday = 0, so numeric weekdays are rewritten as names. Pure."""
    minute, hour, dom, month, dow = expr.split()
    if dow != "*":
        def name(tok: str) -> str:
            return "-".join(_DOW[x] for x in tok.split("-"))
        dow = ",".join(name(t) for t in dow.split(","))
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=dow, timezone="UTC")


def _fires(trigger: CronTrigger, start: datetime, days: int = 7) -> set[datetime]:
    out, t, end = set(), start, start + timedelta(days=days)
    while True:
        t = trigger.get_next_fire_time(None, t)
        if t is None or t >= end:
            return out
        out.add(t.astimezone(timezone.utc))
        t = t + timedelta(seconds=1)


@pytest.mark.skipif(not WORKFLOWS.is_dir(), reason="ops/ not in this checkout (the public mirror)")
def test_dispatch_slots_match_each_workflows_own_cron():
    """GHA_DISPATCH (PKT) and each workflow's `schedule:` (UTC) must fire at the same instants, else the
    clock and its backstop drift apart. mufap: the `set` input must match the cron's job (03 UTC = weekly)."""
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)  # a Monday
    by_wf: dict[tuple[str, str], set[datetime]] = {}
    for d in GHA_DISPATCH:
        key = (d.workflow, d.inputs.get("set", ""))
        by_wf.setdefault(key, set()).update(_fires(CronTrigger(**d.cron, timezone="Asia/Karachi"), start))
    for (wf, kind), fires in by_wf.items():
        doc = yaml.safe_load((WORKFLOWS / wf).read_text(encoding="utf-8"))
        on = doc.get("on", doc.get(True))  # PyYAML reads the bare key `on` as True
        crons = [c["cron"] for c in on["schedule"]]
        if wf == "mufap.yml":
            crons = [c for c in crons if c.startswith("0 3 ") == (kind == "weekly")]
        want = set().union(*(_fires(_crontab_trigger(c), start) for c in crons))
        assert fires == want, f"{wf} {kind}: dispatch {sorted(fires - want)} vs cron {sorted(want - fires)}"
        assert "gate" in doc["jobs"], f"{wf}: no backstop gate job"


def test_payload_sends_strings():
    assert gd.payload({"set": "daily", "clock": True, "n": 3}) == {
        "ref": "main", "inputs": {"set": "daily", "clock": "true", "n": "3"}}


class _R:
    def __init__(self, code, text=""):
        self.status_code, self.text = code, text


def test_dispatch_retries_server_errors_and_redacts_the_token(monkeypatch):
    monkeypatch.setenv("GHA_DISPATCH_TOKEN", "tok123")
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers["Authorization"]))
        return _R(502, "bad gateway") if len(calls) < 3 else _R(204)

    assert gd.dispatch("ingest.yml", {}, post=post, sleep=lambda s: None) is None
    assert len(calls) == 3
    assert calls[0][0].endswith("/repos/amaanjbutt/pakdata-ingest/actions/workflows/ingest.yml/dispatches")
    assert calls[0][2] == "Bearer tok123"

    def bad(url, json=None, headers=None, timeout=None):
        return _R(401, "Bad credentials tok123")

    err = gd.dispatch("ingest.yml", {}, post=bad, sleep=lambda s: None)
    assert err.startswith("HTTP 401") and "tok123" not in err  # no retry on 4xx, token never echoed


def test_dispatch_network_error_then_gives_up(monkeypatch):
    monkeypatch.setenv("GHA_DISPATCH_TOKEN", "ghp_fake0000")

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    assert "ConnectError" in gd.dispatch("pbs.yml", {}, post=boom, sleep=lambda s: None)


def test_token_falls_back_to_the_scheduler_pat(monkeypatch):
    monkeypatch.delenv("GHA_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("REQLOG_ARCHIVE_TOKEN", raising=False)
    assert not gd.configured()
    monkeypatch.setenv("REQLOG_ARCHIVE_TOKEN", "pat")
    assert gd.token() == "pat"
    monkeypatch.setenv("GHA_DISPATCH_TOKEN", "narrow")
    assert gd.token() == "narrow"


class _Redis:
    def __init__(self):
        self.keys = {}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.keys:
            return False
        self.keys[k] = v
        return True

    def delete(self, k):
        self.keys.pop(k, None)


def test_run_alerts_once_and_rearms(monkeypatch):
    sent = []
    monkeypatch.setattr("ingestion.alerting.alert", lambda t, m: sent.append(t))
    results = iter(["HTTP 401: Bad credentials", "HTTP 401: Bad credentials", None, "HTTP 404: Not Found"])
    monkeypatch.setattr(gd, "dispatch", lambda wf, inputs=None: next(results))
    r = _Redis()
    assert gd.run("ingest.yml", {}, r) is False
    assert gd.run("ingest.yml", {}, r) is False      # throttled
    assert gd.run("ingest.yml", {}, r) is True       # recovered → re-armed
    assert gd.run("ingest.yml", {}, r) is False
    assert sent == ["GitHub Actions dispatch failed: ingest.yml"] * 2
