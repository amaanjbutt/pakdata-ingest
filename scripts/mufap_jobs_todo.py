"""Print the daily MUFAP jobs that still need a run in this slot (space-separated).

Used by mufap.yml after a runner gets a good IP and opens the DB tunnel. A job is
done for the slot once it has a `success` / `no_new_data` run in the last
WINDOW_MINUTES; a `failed` (403) or `partial` run leaves it on the list, so a
later attempt in the same dispatch retries just that job.

With a bundle dir, a page-backed job runs only if its page was bundled. MUFAP often
lets a runner fetch tab=1 and then 403s the rest; a job whose page is missing would
go live, hit the same 403 and write a `failed` run (2026-10-20). Left on the list,
a later runner with a cleaner IP picks it up.

    python -m scripts.mufap_jobs_todo [BUNDLE_DIR]
"""
from __future__ import annotations

import sys

# Bundle-backed jobs first (their pages were fetched before the IP could be
# re-blocked), then the debt-file jobs, which use a different, less-blocked endpoint.
DAILY_JOBS = [
    "mufap_fund_navs", "mufap_fund_returns", "mufap_fund_stats",
    "mufap_pkrv", "mufap_debt_prices", "mufap_tfc_valuations", "mufap_debt_trades",
]
WINDOW_MINUTES = 150  # slots are 3h apart (14/17/20 UTC)


def required_pages() -> dict[str, str]:
    """job -> the bundled page it cannot run without (stats' payouts page is optional)."""
    from ingestion.jobs.mufap_fund_navs import NAV_URL
    from ingestion.jobs.mufap_fund_returns import RETURNS_URL
    from ingestion.jobs.mufap_fund_stats import EXPENSES_URL

    return {"mufap_fund_navs": NAV_URL, "mufap_fund_returns": RETURNS_URL, "mufap_fund_stats": EXPENSES_URL}


def jobs_todo(done: set[str], bundled: set[str] | None = None,
              pages: dict[str, str] | None = None) -> list[str]:
    """Pure: DAILY_JOBS minus the ones already done, in run order. With `bundled`
    (the URLs in the bundle), page-backed jobs whose page is missing are held back."""
    out = [j for j in DAILY_JOBS if j not in done]
    if bundled is not None:
        pages = required_pages() if pages is None else pages
        out = [j for j in out if j not in pages or pages[j] in bundled]
    return out


def _done_this_slot() -> set[str]:
    from app import db

    rows = db.query(
        "SELECT DISTINCT job_name FROM ingestion_runs "
        "WHERE job_name = ANY(%(jobs)s) AND status IN ('success', 'no_new_data') "
        "AND started_at > now() - make_interval(mins => %(mins)s)",
        {"jobs": DAILY_JOBS, "mins": WINDOW_MINUTES},
    )
    return {r["job_name"] for r in rows}


def _bundled(d: str) -> set[str]:
    import json
    from pathlib import Path

    try:
        return set(json.loads((Path(d) / "manifest.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


if __name__ == "__main__":
    bundle = _bundled(sys.argv[1]) if len(sys.argv) > 1 else None
    print(" ".join(jobs_todo(_done_this_slot(), bundle)))
