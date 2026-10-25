"""One-time backfill of deep fund payout history from MUFAP.

MUFAP's payouts report (IndustryStatDaily tab=4) is date-ranged via
`datefrom`/`datetill` — with a window it returns every payout event, not just the
latest per fund. The scheduled job (`mufap_fund_stats`) fetches a rolling recent
window; this seeds the full back-history once, year by year (each year is a
manageable response; the whole range at once times out). Additive upsert into
`fund_payouts` — safe to re-run. Run on the VPS (WARP egress):

    PYTHONPATH=/app python scripts/backfill_payouts.py [start_year]
"""
from __future__ import annotations

import sys
import time
from datetime import date

from app import db
from ingestion import http_client
from ingestion.jobs.mufap_fund_stats import parse_payouts_html, payouts_url


def _known_fund_ids() -> set[int]:
    return {r["fund_id"] for r in db.query("SELECT fund_id FROM funds")}


def _upsert(rows, known: set[int]) -> int:
    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                if r.payout_date is None or r.fund_id not in known:
                    continue
                cur.execute(
                    """INSERT INTO fund_payouts (fund_id, payout_date, per_unit, ex_nav, revised_at)
                       VALUES (%s, %s, %s, %s, now())
                       ON CONFLICT (fund_id, payout_date)
                       DO UPDATE SET per_unit = EXCLUDED.per_unit, ex_nav = EXCLUDED.ex_nav,
                                     revised_at = now()""",
                    (r.fund_id, r.payout_date, r.per_unit, r.ex_nav),
                )
                n += 1
    return n


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 2010
    end = date.today().year
    known = _known_fund_ids()
    for yr in range(start, end + 1):
        url = payouts_url(date(yr, 1, 1), date(yr, 12, 31))
        for attempt in range(1, 4):
            try:
                html = http_client.get(url, timeout=150).content
                try:
                    rows = parse_payouts_html(html.decode("utf-8", errors="replace"))
                except ValueError:
                    rows = []  # no payouts table / empty year
                n = _upsert(rows, known)
                print(f"{yr}: parsed={len(rows)} upserted={n}", flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"{yr} attempt {attempt} FAIL {type(exc).__name__}: {str(exc)[:90]}", flush=True)
                time.sleep(8)
        time.sleep(2)
    tot = db.query_one("SELECT count(*) c, count(distinct fund_id) f FROM fund_payouts")
    print(f"DONE total_payouts={tot['c']} funds={tot['f']}", flush=True)


if __name__ == "__main__":
    main()
