"""Feasibility probe: does MUFAP's fund-detail POST honor a historical `Date`?

If passing Date=2026-03-01 returns that month's Table2 (as_of March), historical
monthly allocations are available and a backfill is worth building. If it always
returns the latest snapshot regardless of Date, they are not. Prints one line per
requested date with the actual Table2 as_of; prints `PROBE_OK` if any fetch
succeeded (so a driver can stop retrying). Run on the VPS (WARP).
"""
from __future__ import annotations


from ingestion import http_client
from ingestion.jobs.mufap_fund_portfolio import DETAIL_URL, parse_portfolio

FUND = 12840  # Meezan Gold — a distinctive allocation (gold ~86%)
DATES = ["05/01/2026", "03/01/2026", "06/01/2025", "06/01/2024"]


def main() -> None:
    ok = False
    for d in DATES:
        try:
            r = http_client.post(DETAIL_URL, json={"FundID": FUND, "Date": d}, timeout=60)
            as_of, alloc = parse_portfolio(r.text)
            total = round(sum(p for _, p in alloc), 1) if alloc else None
            print(f"requested {d} -> as_of={as_of} lines={len(alloc)} sum={total}", flush=True)
            ok = True
        except Exception as exc:  # noqa: BLE001
            print(f"requested {d} -> FAIL {type(exc).__name__}", flush=True)
    print("PROBE_OK" if ok else "PROBE_FAIL", flush=True)


if __name__ == "__main__":
    main()
