"""Edge health (scripts/edge_health.py): Cloudflare's daily figures, uptime probes, the daily email.

Fixtures are Cloudflare GraphQL answers for two real days: 2026-11-11, when crawlers (DataForSeoBot on
/mandi/* and /coverage) drove the website Worker into the free plan's CPU limit 4,417 times, and
2026-11-13, a quiet day whose only 5xx were bare /_serverFn/ GETs from OAI-SearchBot and two MCP probes.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import edge_health as eh

FIX = Path(__file__).parent / "fixtures"


def _day(d: str) -> dict:
    return json.loads((FIX / f"cf_edge_daily_{d}.json").read_text())


def test_summarize_storm_day():
    s = eh.summarize(_day("2026-11-11"))
    assert s["worker_requests"] == 4417 + 37 + 29387
    assert s["exceeded_resources"] == 4417
    assert s["client_disconnected"] == 37
    assert s["worker_by_status"]["success"] == 29387
    # visitors saw the CPU-limit kills as 503s (sample-weighted, so a little above the raw 4,424 rows)
    assert 4424 <= s["site_by_status"]["503"] <= 4500
    assert s["site_5xx"] == s["site_by_status"]["503"] + s["site_by_status"]["500"]
    assert s["cpu_p50_ms"] == 18.7 and s["cpu_p99_ms"] == 181.7
    assert s["api_requests"] == 0  # api.pakdatahub.com is not proxied by Cloudflare
    assert s["top_5xx"][0] == {"host": "pakdatahub.com", "path": "/coverage", "status": 503, "count": 214}
    found = eh.problems(s)
    assert any("CPU limit" in p for p in found)
    assert any("5xx" in p for p in found)
    assert not any("free plan's 100,000" in p for p in found)  # 34k requests: well inside the cap


def test_summarize_quiet_day_has_no_problems():
    s = eh.summarize(_day("2026-11-13"))
    assert s["exceeded_resources"] == 0
    assert s["site_by_status"]["500"] == 18
    assert s["top_5xx"][0]["path"].startswith("/_serverFn/")
    assert eh.problems(s) == []


def test_problems_thresholds():
    base = eh.summarize(_day("2026-11-13"))
    assert eh.problems({**base, "worker_requests": 85_000})[0].startswith("85,000 Worker requests")
    assert eh.problems({**base, "exceeded_resources": eh.CPU_LIMIT_ALERT}) == []  # at the threshold: fine
    assert len(eh.problems({**base, "exceeded_resources": eh.CPU_LIMIT_ALERT + 1})) == 1


def test_check_response():
    page = "<!doctype html><html><head><title>x</title></head><body>ok</body></html>"
    assert eh.check_response("page", 200, page) is None
    assert eh.check_response("page", 503, page) == "HTTP 503"
    assert eh.check_response("page", 200, page[:40]) == "HTTP 200 but not a whole page"
    assert eh.check_response("api", 200, '{"status":"ok"}') is None
    assert eh.check_response("api", 200, "<html>") == "HTTP 200 but not JSON"


def test_probe_ua_is_not_a_crawler():
    # frontend/src/lib/pakdata/bot-ua.ts sends crawlers the light page; the probe must get people's page
    generic = re.compile(r"bot|crawl|spider|slurp|fetch|preview|scan|monitor", re.I)
    assert not generic.search(eh.PROBE_UA)


class _Redis:
    def __init__(self):
        self.h: dict[str, dict[str, str]] = {}
        self.kv: dict[str, str] = {}

    def hincrby(self, k, f, n):
        d = self.h.setdefault(k, {})
        d[f] = str(int(d.get(f, 0)) + n)

    def hget(self, k, f):
        return self.h.get(k, {}).get(f)

    def hset(self, k, f, v):
        self.h.setdefault(k, {})[f] = str(v)

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def incr(self, k):
        self.kv[k] = str(int(self.kv.get(k, 0)) + 1)
        return int(self.kv[k])

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = v
        return True

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None)

    def expire(self, k, ttl):
        return True


class _Resp:
    def __init__(self, status, text, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


def _getter(down: set[str]):
    page = "<html><head><title>t</title></head><body></body></html>"

    def get(url):
        if any(url.endswith(d) for d in down):
            return _Resp(503, "error code: 1102")
        return _Resp(200, '{"ok":1}' if "api." in url else page, {"x-edge-cache": "HIT"})
    return get


def test_probe_alerts_on_second_failure_once_then_rearms(monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr("ingestion.alerting.alert", lambda t, m: alerts.append(t))
    r = _Redis()
    now = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
    res = eh.probe(r, get=_getter(set()), now=now)
    assert len(res) == len(eh.KEY_PAGES) + len(eh.API_ENDPOINTS) and all(x["ok"] for x in res)

    eh.probe(r, get=_getter({"/coverage"}), now=now)
    assert alerts == []  # one failure: could be a blip
    eh.probe(r, get=_getter({"/coverage"}), now=now)
    assert alerts == ["/coverage is failing"]
    eh.probe(r, get=_getter({"/coverage"}), now=now)
    assert alerts == ["/coverage is failing"]  # throttled
    eh.probe(r, get=_getter(set()), now=now)  # recovers: re-armed
    eh.probe(r, get=_getter({"/coverage"}), now=now)
    eh.probe(r, get=_getter({"/coverage"}), now=now)
    assert alerts == ["/coverage is failing"] * 2

    summary = eh.probe_summary(r, now=now)
    assert summary["/coverage"]["n"] == 7 and summary["/coverage"]["fail"] == 5
    assert summary["api /health"] == {"n": 7, "fail": 0, "max_ms": summary["api /health"]["max_ms"],
                                      "avg_ms": summary["api /health"]["avg_ms"]}


def test_render_email_all_clear_and_issues():
    probes = {t: {"n": 288, "fail": 0, "avg_ms": 120, "max_ms": 900} for t, _, _ in eh.targets()}
    quiet = {**eh.summarize(_day("2026-11-13")), "day": date(2026, 11, 13)}
    data = {"day": date(2026, 11, 13), "edge": quiet, "probes": probes, "runs": [
        {"job_name": "amis_prices", "runs": 1, "failed": 0, "partial": 0, "latest_status": "success", "latest_error": None},
    ], "stale": {}, "external_jobs": ["kpt_shipping"], "gha": [], "alerts": {}}
    subject, body, n = eh.render_email(data)
    assert n == 0 and subject == "PakDataHub health · Nov 13: all clear"
    assert "All clear" in body and "27,034" in body and "21,771 (22 %" in body  # visitors; Worker vs the cap

    storm = {**eh.summarize(_day("2026-11-11")), "day": date(2026, 11, 11)}
    probes["/mandi"] = {"n": 288, "fail": 3, "avg_ms": 400, "max_ms": 20000}
    data.update(edge=storm, probes=probes, stale={"kpt_shipping": "Last fresh run 4 days ago"},
                runs=data["runs"] + [{"job_name": "kpt_shipping", "runs": 2, "failed": 2, "partial": 0,
                                      "latest_status": "failed", "latest_error": "date <script> in the future"}],
                gha=[{"name": "ports", "status": "completed", "conclusion": "failure", "event": "workflow_dispatch",
                      "url": "https://github.com/x/y/actions/runs/1", "created_at": "2026-11-13T05:30:00Z"}],
                alerts={"kpt_shipping: GitHub Actions run problem": 1})
    subject, body, n = eh.render_email(data)
    assert n == 6  # CPU limit, 5xx rate, /mandi probes, kpt failed, kpt late, ports workflow
    assert subject.endswith("6 to look at")
    assert "&lt;script&gt;" in body and "<script>" not in body  # errors are escaped
    assert "GitHub" in body and "/coverage" in body


from tests.test_api_integration import _db_available  # noqa: E402


@pytest.mark.skipif(not _db_available(), reason="no database")
def test_store_day_upserts_one_row():
    from app import db

    ddl = (Path(__file__).parents[1] / "db" / "schema.sql").read_text()
    db.execute(re.search(r"CREATE TABLE IF NOT EXISTS ops_edge_daily \(.*?\n\);", ddl, re.S).group(0))
    day = date(1999, 1, 1)
    try:
        eh.store_day(day, eh.summarize(_day("2026-11-11")))
        eh.store_day(day, eh.summarize(_day("2026-11-13")))  # a re-run replaces the row
        rows = db.query("SELECT * FROM ops_edge_daily WHERE day = %s", (day,))
        assert len(rows) == 1
        assert rows[0]["exceeded_resources"] == 0 and rows[0]["site_by_status"]["500"] == 18
        assert rows[0]["top_5xx"][0]["path"].startswith("/_serverFn/")
    finally:
        db.execute("DELETE FROM ops_edge_daily WHERE day = %s", (day,))
