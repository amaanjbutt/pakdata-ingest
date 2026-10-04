"""Fetch every MUFAP IndustryStatDaily page the daily jobs need, in one burst.

Run first on a GitHub runner, before the DB tunnel opens. MUFAP's Cloudflare
lets only a few Azure IPs through, and even those get re-blocked after a few
requests. Burst-fetching the pages while the IP is clean, then letting the jobs
read them from disk (INGEST_BUNDLE_DIR, see ingestion/http_bundle.py), gets
everything from one good IP. Before this, the slow DB writes between fetches
let the block land: navs succeeded, then returns 403'd on the same page the
probe had just fetched.

The first page doubles as the IP probe.

    python -m scripts.mufap_fetch_bundle /tmp/raw/mufap_bundle

Exit codes: 0 = probe page fetched (bundle may still be partial; missing pages
fall through to a live fetch in the job), 2 = IP blocked (nothing fetched).
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta
from pathlib import Path

from ingestion import http_bundle, http_client
from ingestion.jobs.mufap_fund_navs import NAV_URL
from ingestion.jobs.mufap_fund_returns import RETURNS_URL
from ingestion.jobs.mufap_fund_stats import EXPENSES_URL, PAYOUTS_WINDOW_DAYS, payouts_url


def bundle_urls(today: date) -> list[str]:
    """The pages to fetch, probe first. The payouts URL must equal the one
    mufap_fund_stats builds on the same day, or the job falls back to a live fetch."""
    return [
        RETURNS_URL,   # tab=1 — also the IP probe
        NAV_URL,       # tab=3
        EXPENSES_URL,  # tab=5
        payouts_url(today - timedelta(days=PAYOUTS_WINDOW_DAYS), today),  # tab=4
    ]


def _fetch_once(url: str) -> tuple[int | None, bytes | None]:
    """One request, plus one retry on a fresh session. No long 403 backoff — a
    blocked IP should exit fast."""
    for attempt in range(2):
        try:
            resp = http_client._sess().get(
                url, proxies=http_client._proxies_for(url), timeout=60
            )
        except Exception as exc:  # noqa: BLE001 - network stall counts as blocked
            print(f"  error: {exc}")
            return None, None
        if resp.status_code == 200 and resp.content:
            return 200, resp.content
        if attempt == 0:
            http_client.reset_session()
            time.sleep(3)
    return resp.status_code, None


def main(argv: list[str]) -> int:
    out = Path(argv[1] if len(argv) > 1 else "/tmp/raw/mufap_bundle")
    got = 0
    for i, url in enumerate(bundle_urls(date.today())):
        t0 = time.monotonic()
        status, content = _fetch_once(url)
        dt = time.monotonic() - t0
        if content is None:
            print(f"{status} {url} ({dt:.1f}s) — not bundled")
            if i == 0:
                return 2  # probe failed: this IP is blocked
            continue
        http_bundle.save(out, url, content)
        got += 1
        print(f"200 {url} {len(content):,} bytes ({dt:.1f}s) — bundled")
    print(f"bundled {got} page(s) into {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
