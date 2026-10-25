"""Backfill historical fund RETURNS (tab=1) and EXPENSES (tab=5) snapshots.

Same discovery as payouts: MUFAP's IndustryStatDaily tabs accept
`datefrom`/`datetill`. Returns and expenses are point-in-time snapshots keyed by
a validity date; a date range *should* return every archived snapshot in the
window (a time series), not just the latest. The `dates=` count printed per year
reveals whether MUFAP actually archives history (many dates) or only serves the
latest (one date) — so the first successful year is also the feasibility test.

Reuses the existing parsers + upserts. Additive, resumable. Run on the VPS (WARP):
    PYTHONPATH=/app python scripts/backfill_fund_history.py [start_year]
"""
from __future__ import annotations

import sys
import time
from datetime import date

from ingestion import http_client
from ingestion.jobs.mufap_fund_returns import MufapFundReturnsJob, parse_returns_html
from ingestion.jobs.mufap_fund_stats import MufapFundStatsJob, parse_expenses_html

BASE = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=%d&AMCId=null&fundId=null&datefrom=%s&datetill=%s"


def _url(tab: int, yr: int) -> str:
    return BASE % (tab, f"{yr}-01-01", f"{yr}-12-31")


def _fetch(tab: int, yr: int, parser):
    """Fetch+parse one year for a tab; returns (rows, distinct_dates) or None on fail."""
    for attempt in range(1, 4):
        try:
            html = http_client.get(_url(tab, yr), timeout=150).content
            try:
                rows = parser(html.decode("utf-8", errors="replace"))
            except ValueError:
                rows = []
            dates = len({r.obs_date for r in rows if r.obs_date})
            return rows, dates
        except Exception as exc:  # noqa: BLE001
            print(f"  tab{tab} {yr} attempt {attempt} FAIL {type(exc).__name__}: {str(exc)[:70]}", flush=True)
            time.sleep(8)
    return None


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 2015
    end = date.today().year
    rjob, sjob = MufapFundReturnsJob(), MufapFundStatsJob()
    for yr in range(start, end + 1):
        r = _fetch(1, yr, parse_returns_html)
        if r is not None:
            rows, dates = r
            n = rjob._upsert(rows)
            print(f"returns  {yr}: rows={len(rows)} dates={dates} upserted={n}", flush=True)
        time.sleep(2)
        e = _fetch(5, yr, parse_expenses_html)
        if e is not None:
            rows, dates = e
            n = sjob._upsert_expenses(rows)
            print(f"expenses {yr}: rows={len(rows)} dates={dates} upserted={n}", flush=True)
        time.sleep(2)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
