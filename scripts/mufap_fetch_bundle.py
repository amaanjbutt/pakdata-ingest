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
    python -m scripts.mufap_fetch_bundle --files-probe

Exit codes: 0 = probe page fetched (bundle may still be partial; missing pages
fall through to a live fetch in the job), 2 = IP blocked (nothing fetched).

`--files-probe`: since 2026-11-10 MUFAP's fund pages (/Industry/*, /AMC/*) answer every
non-browser client with a Cloudflare managed challenge (the PC too), while the file API
(POST /WebRegulations/GetSecpFileById) and the /Upload/ files it lists are served plainly.
The four file jobs (PKRV, debt prices, TFC valuations, debt trades) need only those, so a
runner whose page probe failed checks the file API instead of giving up: exit 0 = the list
and its newest file came back, 2 = not.
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


FILES_LIST_URL = "https://www.mufap.com.pk/WebRegulations/GetSecpFileById"
FILES_BASE = "https://www.mufap.com.pk"
FILES_PROBE_TAB = 46  # PKRV / PKISRV: a new file every business day


def file_reachable(status: int | None, content: bytes | None) -> bool:
    """A real file, not a 403 or Cloudflare's 'Just a moment...' challenge page. Pure."""
    if status != 200 or not content:
        return False
    head = content[:2048].lower()
    return b"just a moment" not in head and b"challenges.cloudflare.com" not in head


def files_probe() -> int:
    """0 if MUFAP's file API answers this runner (list + its newest file), else 2."""
    try:
        resp = http_client.post(FILES_LIST_URL, json={"fk_HeaderSubMenuTabId": FILES_PROBE_TAB}, timeout=45)
        items = (resp.json().get("data") or []) if resp.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001 - a stall or an HTML challenge counts as blocked
        print(f"file list: error {exc}")
        return 2
    if not items:
        print(f"file list: {getattr(resp, 'status_code', None)} with no files — blocked")
        return 2
    newest = items[-1]  # the list is oldest first
    try:
        f = http_client._sess().get(FILES_BASE + newest["FilePath"],
                                    proxies=http_client._proxies_for(FILES_BASE), timeout=60)
        status, content = f.status_code, f.content
    except Exception as exc:  # noqa: BLE001
        print(f"file {newest.get('Title')}: error {exc}")
        return 2
    ok = file_reachable(status, content)
    print(f"file list {len(items)} entries; {newest.get('Title')}: {status} {len(content or b''):,} bytes"
          f" — {'reachable' if ok else 'blocked'}")
    return 0 if ok else 2


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--files-probe":
        return files_probe()
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
