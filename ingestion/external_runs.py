"""Alert on bad runs of the jobs the VPS doesn't run itself (GitHub Actions, see MONITORED_EXTERNAL).

An alert raised inside a GitHub Actions run is only logged: the public runner repo holds no
ALERT_EMAIL_TO / Resend key, and the workflows keep going past a failed job (`set +e`), so GitHub
sends no failure email either. The staleness check catches a job that stops landing data, but not
one that lands *some*: from early November 2026 one EasyData key answered 401 on every run, cost
each run 225 skipped series, and the run still finished `success` with nobody told.

So the VPS reads `ingestion_runs` for those jobs every hour (from `scheduler.health_check`):
- the latest run's error names a rejected key (401) — the owner has to mint a new one;
- the two latest runs both failed — one failure is usually a publisher blip that the next run
  heals (SBP's 503s of 2026-10-30 and 11-02), two in a row is not. MUFAP's Cloudflare 403s are
  left to the staleness check, like `alerting.alert_unless_403`.
Each problem alerts once per job per day and re-arms when the job's latest run is clean.
"""
from __future__ import annotations

import logging
import re

from app import db
from ingestion import alerting

log = logging.getLogger("pakdata.scheduler")

ALERT_TTL_SECONDS = 24 * 3600
_KEY_REJECTED = re.compile(r"\(401\)|Error 401")  # not "401/638 funds", not "record(s) rejected"


def problem(runs: list[dict]) -> str | None:
    """What is wrong with a job, from its runs newest first ({status, error}), or None. Pure."""
    done = [r for r in runs if r.get("status") != "running"]
    if not done:
        return None
    latest = done[0]
    err = latest.get("error") or ""
    if _KEY_REJECTED.search(err):
        return f"latest run ({latest.get('status')}): {err}"
    if len(done) >= 2 and all(r.get("status") == "failed" for r in done[:2]):
        errs = [r.get("error") or "" for r in done[:2]]
        if any("403" in e for e in errs):
            return None  # MUFAP's Cloudflare: self-healing, the staleness check owns it
        return f"the last two runs failed; latest error: {errs[0] or '(none recorded)'}"
    return None


def _recent_runs(jobs: list[str]) -> dict[str, list[dict]]:
    rows = db.query(
        """
        SELECT job_name, status, error FROM (
            SELECT job_name, status, error,
                   row_number() OVER (PARTITION BY job_name ORDER BY started_at DESC) AS rn
            FROM ingestion_runs
            WHERE job_name = ANY(%(jobs)s) AND started_at > now() - interval '14 days'
        ) x WHERE rn <= 3 ORDER BY job_name, rn
        """,
        {"jobs": jobs},
    )
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["job_name"], []).append(r)
    return out


def external_run_check(redis_client) -> dict[str, str]:
    """Alert (throttled) on each MONITORED_EXTERNAL job whose recent runs look wrong."""
    from ingestion.schedule import MONITORED_EXTERNAL

    jobs = sorted({s.job for s in MONITORED_EXTERNAL})
    found: dict[str, str] = {}
    for job, runs in _recent_runs(jobs).items():
        msg = problem(runs)
        key = f"pakdata:extrunalert:{job}"
        if msg is None:
            try:
                redis_client.delete(key)
            except Exception:  # noqa: BLE001
                pass
            continue
        found[job] = msg
        try:
            first = redis_client.set(key, "1", nx=True, ex=ALERT_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 — fail open, like the staleness alert
            log.warning("external-run alert throttle unavailable (%s); alerting anyway", exc)
            first = True
        if first:
            alerting.alert(f"{job}: GitHub Actions run problem", msg)
        else:
            log.info("external runs: %s still failing (alert throttled)", job)
    return found
