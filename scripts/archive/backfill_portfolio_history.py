"""Backfill historical MONTHLY portfolio allocations.

The fund-detail POST honors a historical `Date` — requesting the first of a month
returns that month's Table2 snapshot (confirmed: 04/2026→as_of 2026-04-01, etc.).
This gap-fills per (fund, month) over the last MONTHS_BACK months, additively
(new months never touch the latest snapshot), with the reconciliation gate in
parse_portfolio dropping bad-source months. Resumable; aborts a pass on a failure
streak (exit 2) so the driver can rotate WARP and resume. Run on the VPS (WARP).
"""
from __future__ import annotations

from datetime import date

from app import db
from ingestion import http_client
from ingestion.jobs.mufap_fund_portfolio import DETAIL_URL, parse_portfolio

MONTHS_BACK = 24
_FAIL_STREAK_ABORT = 12
import time


def target_months() -> list[date]:
    today = date.today()
    y, m = today.year, today.month
    out = []
    for i in range(1, MONTHS_BACK + 1):
        mm, yy = m - i, y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append(date(yy, mm, 1))
    return out


def _existing_months(fund_id: int) -> set:
    return {r["as_of_date"] for r in db.query(
        "SELECT DISTINCT as_of_date FROM fund_portfolio WHERE fund_id=%s", (fund_id,))}


def _upsert(fund_id: int, as_of: date, alloc) -> int:
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO fund_portfolio (fund_id, as_of_date, asset_class, percent, revised_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (fund_id, as_of_date, asset_class)
                   DO UPDATE SET percent=EXCLUDED.percent, revised_at=now()""",
                [(fund_id, as_of, c, p) for c, p in alloc],
            )
    return len(alloc)


def main() -> None:
    fund_ids = [r["fund_id"] for r in db.query(
        "SELECT fund_id FROM funds WHERE is_active=true ORDER BY fund_id")]
    months = target_months()
    total = filled = streak = 0
    for fid in fund_ids:
        have = _existing_months(fid)
        for mon in months:
            if mon in have:
                continue
            try:
                r = http_client.post(DETAIL_URL, json={"FundID": fid, "Date": mon.strftime("%m/%d/%Y")}, timeout=60)
                as_of, alloc = parse_portfolio(r.text)
                streak = 0
            except Exception:  # noqa: BLE001
                streak += 1
                if streak >= _FAIL_STREAK_ABORT:
                    print(f"ABORT streak filled={filled} rows={total}", flush=True)
                    raise SystemExit(2)
                time.sleep(0.5)
                continue
            if as_of and alloc:
                total += _upsert(fid, as_of, alloc)
                have.add(as_of)
                filled += 1
            time.sleep(0.3)
    print(f"DONE filled={filled} rows={total}", flush=True)


if __name__ == "__main__":
    main()
