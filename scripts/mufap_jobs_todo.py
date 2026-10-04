"""Print the daily MUFAP jobs that still need a run in this slot (space-separated).

Used by mufap.yml after a runner gets a good IP and opens the DB tunnel. A job is
done for the slot once it has a `success` / `no_new_data` run in the last
WINDOW_MINUTES; a `failed` (403) or `partial` run leaves it on the list, so a
later attempt in the same dispatch retries just that job.

    python -m scripts.mufap_jobs_todo
"""
from __future__ import annotations

# Bundle-backed jobs first (their pages were fetched before the IP could be
# re-blocked), then the debt-file jobs, which use a different, less-blocked endpoint.
DAILY_JOBS = [
    "mufap_fund_navs", "mufap_fund_returns", "mufap_fund_stats",
    "mufap_pkrv", "mufap_debt_prices", "mufap_tfc_valuations", "mufap_debt_trades",
]
WINDOW_MINUTES = 150  # slots are 3h apart (14/17/20 UTC)


def jobs_todo(done: set[str]) -> list[str]:
    """Pure: DAILY_JOBS minus the ones already done, in run order."""
    return [j for j in DAILY_JOBS if j not in done]


def _done_this_slot() -> set[str]:
    from app import db

    rows = db.query(
        "SELECT DISTINCT job_name FROM ingestion_runs "
        "WHERE job_name = ANY(%(jobs)s) AND status IN ('success', 'no_new_data') "
        "AND started_at > now() - make_interval(mins => %(mins)s)",
        {"jobs": DAILY_JOBS, "mins": WINDOW_MINUTES},
    )
    return {r["job_name"] for r in rows}


if __name__ == "__main__":
    print(" ".join(jobs_todo(_done_this_slot())))
