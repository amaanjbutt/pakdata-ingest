"""Refresh EasyData series NAMES + DESCRIPTIONS from SBP dataset metadata.

Why: the original discovery stored SBP's bare leaf names ("Between fellow
enterprises") with no dataset context, and often blank descriptions — so many
pages are unreadable. SBP's dataset `/meta` endpoint carries a "Reference Dataset
Name" that gives the missing context. This job re-fetches every dataset's meta
(browser-TLS), recomposes clear names via `discover_easydata.compose_name` /
`compose_description`, and stages the result in `series_meta_staging`.

It does NOT touch the live catalog. The copy-then-swap into `series` (name +
description only — never ids, values, or observations) is a separate, verified
step:

    -- verify staging first, then:
    CREATE TABLE series_meta_bak_<ts> AS
      SELECT id, name, description FROM series
      WHERE id IN (SELECT id FROM series_meta_staging);
    UPDATE series s SET name = st.name, description = st.description
      FROM series_meta_staging st WHERE s.id = st.id;

Run where SBP is reachable (GitHub Actions / Azure) with DATABASE_URL +
EASYDATA_API_KEY(S) set.
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import defaultdict

from app import db
from app.db import connection
from ingestion import http_client
from ingestion.discover_easydata import parse_dataset_meta

META_URL = "https://easydata.sbp.org.pk/api/v1/dataset/{code}/meta?api_key={key}&format=json"
KEYS = [
    k
    for k in re.split(
        r"[,\s]+", (os.getenv("EASYDATA_API_KEYS") or os.getenv("EASYDATA_API_KEY") or "").strip()
    )
    if k
]


def _key_from_url(url: str | None) -> str | None:
    m = re.search(r"/series/([^/]+)/data", url or "")
    return m.group(1) if m else None


def main() -> None:
    if not KEYS:
        sys.exit("EASYDATA_API_KEY(S) not set")
    rows = db.query(
        "SELECT id, source_url, easydata_dataset_code AS code FROM series "
        "WHERE easydata_dataset_code IS NOT NULL AND is_active"
    )
    by_ds: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        k = _key_from_url(r["source_url"])
        if k:
            by_ds[r["code"]].append((r["id"], k))
    print(f"read {len(rows)} EasyData series across {len(by_ds)} datasets", flush=True)

    db.execute(
        "CREATE TABLE IF NOT EXISTS series_meta_staging "
        "(id text PRIMARY KEY, name text, description text)"
    )
    db.execute("TRUNCATE series_meta_staging")

    staged = failed = 0
    datasets = sorted(by_ds.items())
    with connection() as conn:
        cur = conn.cursor()
        for i, (code, members) in enumerate(datasets):
            key = KEYS[i % len(KEYS)]  # round-robin across keys for headroom
            try:
                resp = http_client.get(META_URL.format(code=code, key=key), timeout=45)
                entries = parse_dataset_meta(resp.content.decode("utf-8", "replace"), code)
            except Exception as e:  # noqa: BLE001 — one bad dataset must not abort the run
                failed += 1
                print(f"  ! {code}: {str(e)[:100]}", flush=True)
                time.sleep(1.5)
                continue
            by_key = {e["easydata_key"]: e for e in entries}
            batch = [
                (sid, by_key[k]["name"], by_key[k]["description"])
                for sid, k in members
                if k in by_key and by_key[k].get("name")
            ]
            if batch:
                cur.executemany(
                    "INSERT INTO series_meta_staging (id, name, description) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, "
                    "description = EXCLUDED.description",
                    batch,
                )
                conn.commit()
                staged += len(batch)
            if i % 20 == 0 or i == len(datasets) - 1:
                print(f"  [{i + 1}/{len(datasets)}] {code}: +{len(batch)} (staged {staged})", flush=True)
            time.sleep(0.4)
    print(f"DONE staged={staged} failed_datasets={failed}", flush=True)


if __name__ == "__main__":
    main()
