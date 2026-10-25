"""One-off: re-parse every fund's asset allocation into a STAGING table.

Copy-then-swap (per the project rule): this writes ONLY to
`fund_portfolio_staging` and never touches the live `fund_portfolio`. Run it on
the VPS (MUFAP egress via WARP). It's resumable — re-running upserts, so a WARP
403 storm mid-run just needs another pass to fill gaps. Verify staging coverage
(funds summing ~100%), then merge into live with merge_fund_portfolio_staging().

    PYTHONPATH=/app python scripts/reparse_fund_portfolio.py
"""
from __future__ import annotations

import time
from datetime import date

from app import db
from ingestion import http_client
from ingestion.jobs.mufap_fund_portfolio import DETAIL_URL, parse_portfolio

STAGING = "fund_portfolio_staging"


def ensure_staging() -> None:
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS {STAGING} (
            fund_id     INTEGER NOT NULL,
            as_of_date  DATE NOT NULL,
            asset_class TEXT NOT NULL,
            percent     NUMERIC,
            revised_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (fund_id, as_of_date, asset_class))"""
    )


# Give up a pass after this many consecutive failures — the WARP IP is burned;
# the host wrapper should rotate it and start a fresh pass (which resumes on the
# still-missing funds). Exit code 2 signals "burned, rotate & retry".
_FAIL_STREAK_ABORT = 12


def main() -> None:
    ensure_staging()
    # Gap-filling: only fetch funds not already staged, so each pass shrinks the
    # remaining work and repeated passes converge quickly.
    fund_ids = [
        r["fund_id"]
        for r in db.query(
            """SELECT fund_id FROM funds WHERE is_active = true
               AND fund_id NOT IN (SELECT DISTINCT fund_id FROM fund_portfolio_staging)
               ORDER BY fund_id"""
        )
    ]
    ok = fail = empty = rows = 0
    streak = 0
    n = len(fund_ids)
    print(f"START pass: {n} funds still missing from staging", flush=True)
    for i, fid in enumerate(fund_ids, 1):
        try:
            resp = http_client.post(
                DETAIL_URL,
                json={"FundID": fid, "Date": date.today().strftime("%m/%d/%Y")},
                timeout=60,
            )
            as_of, alloc = parse_portfolio(resp.text)
            streak = 0
        except Exception as exc:  # noqa: BLE001
            fail += 1
            streak += 1
            print(f"[{i}/{n}] fund {fid} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            if streak >= _FAIL_STREAK_ABORT:
                print(f"ABORT pass: {streak} consecutive failures (IP burned) "
                      f"ok={ok} empty={empty} fail={fail} rows={rows}", flush=True)
                raise SystemExit(2)
            time.sleep(0.5)
            continue
        if as_of and alloc:
            with db.connection() as conn:
                with conn.cursor() as cur:
                    cur.executemany(
                        f"""INSERT INTO {STAGING} (fund_id, as_of_date, asset_class, percent, revised_at)
                            VALUES (%s, %s, %s, %s, now())
                            ON CONFLICT (fund_id, as_of_date, asset_class)
                            DO UPDATE SET percent = EXCLUDED.percent, revised_at = now()""",
                        [(fid, as_of, cls, pct) for cls, pct in alloc],
                    )
            ok += 1
            rows += len(alloc)
        else:
            # Reached MUFAP but no allocation (Total=0 / no Table2). Mark with a
            # sentinel row so gap-filling passes don't retry it forever. Merge and
            # verification ignore `__none__`.
            empty += 1
            db.execute(
                f"INSERT INTO {STAGING} (fund_id, as_of_date, asset_class, percent) "
                f"VALUES (%s, %s, '__none__', NULL) ON CONFLICT DO NOTHING",
                (fid, as_of or date.today()),
            )
        if i % 25 == 0:
            print(f"[{i}/{n}] ok={ok} empty={empty} fail={fail} rows={rows}", flush=True)
        time.sleep(0.3)
    print(f"DONE ok={ok} empty={empty} fail={fail} rows={rows}", flush=True)


if __name__ == "__main__":
    main()
