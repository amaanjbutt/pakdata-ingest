"""The VPS scheduler as the clock for the GitHub Actions ingestion workflows.

GitHub's `schedule:` trigger on the public runner repo amaanjbutt/pakdata-ingest is best-effort, and in
Oct–Nov 2026 it was poor: every workflow started 3–10 h late and about half the slots never ran (`ingest`
2 of 4 a day, `mufap` 1 of 3 weekday slots, `ports`' 10:30 PKT slot at ~18:00). A `workflow_dispatch` starts
at once, so the scheduler sends one at each slot (`schedule.GHA_DISPATCH`, Asia/Karachi times).

The workflows keep their `schedule:` as a backstop: a `gate` job skips a scheduled run when the workflow was
dispatched within its window (a few hours, a week for customs), so while this clock works GitHub's late runs
do nothing, and if it stops (scheduler down, token revoked) the crons take over by themselves.

Token: GHA_DISPATCH_TOKEN, or failing that REQLOG_ARCHIVE_TOKEN — the owner's classic PAT (scopes repo +
workflow, secrets.md → GitHub), which only the scheduler sees (`.env.scheduler`). A fine-grained token with
Actions: read and write on pakdata-ingest alone would be the narrower choice. Unset → nothing is dispatched
and the crons carry on as before.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

import httpx

log = logging.getLogger("pakdata.scheduler")

REPO = os.getenv("GHA_DISPATCH_REPO", "amaanjbutt/pakdata-ingest")
API = "https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches"
ALERT_TTL_SECONDS = 6 * 3600
_RETRY_WAITS = (5, 20)  # seconds before the 2nd and 3rd attempt (5xx / 429 / network only)


def token() -> str:
    return os.getenv("GHA_DISPATCH_TOKEN") or os.getenv("REQLOG_ARCHIVE_TOKEN", "")


def configured() -> bool:
    return bool(token())


def payload(inputs: dict) -> dict:
    """The dispatch body. GitHub wants every input as a string (booleans too). Pure."""
    return {"ref": "main", "inputs": {k: (str(v).lower() if isinstance(v, bool) else str(v))
                                      for k, v in inputs.items()}}


def dispatch(workflow: str, inputs: dict | None = None, *,
             post: Callable = httpx.post, sleep: Callable = time.sleep) -> str | None:
    """Ask GitHub to run `workflow` now. Returns None on success, else the error text."""
    tok = token()
    if not tok:
        return "no GHA_DISPATCH_TOKEN / REQLOG_ARCHIVE_TOKEN"
    url = API.format(repo=REPO, workflow=workflow)
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "pakdatahub-scheduler"}
    err = ""
    for attempt in range(len(_RETRY_WAITS) + 1):
        if attempt:
            sleep(_RETRY_WAITS[attempt - 1])
        try:
            r = post(url, json=payload(inputs or {}), headers=headers, timeout=20)
        except httpx.HTTPError as exc:
            err = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code in (200, 204):
            return None
        err = f"HTTP {r.status_code}: {r.text[:300]}"
        if r.status_code < 500 and r.status_code != 429:
            break  # 401/403/404/422: retrying won't help (token, permissions, workflow name, inputs)
    return err.replace(tok, "***")


def run(workflow: str, inputs: dict | None = None, redis_client=None) -> bool:
    """Scheduler entry point: dispatch, log, and alert (at most once per ALERT_TTL per workflow)."""
    from ingestion import alerting

    err = dispatch(workflow, inputs)
    key = f"pakdata:ghadispatch:{workflow}"
    if err is None:
        log.info("dispatched %s %s", workflow, inputs or {})
        if redis_client is not None:
            try:
                redis_client.delete(key)
            except Exception:  # noqa: BLE001
                pass
        return True
    log.warning("dispatch of %s failed: %s", workflow, err)
    first = True
    if redis_client is not None:
        try:
            first = bool(redis_client.set(key, "1", nx=True, ex=ALERT_TTL_SECONDS))
        except Exception:  # noqa: BLE001 — fail open
            first = True
    if first:
        alerting.alert(f"GitHub Actions dispatch failed: {workflow}",
                       f"{err}\nGitHub's own (late) cron runs it instead until this is fixed — "
                       "check GHA_DISPATCH_TOKEN / REQLOG_ARCHIVE_TOKEN in the VPS .env.scheduler.")
    return False
