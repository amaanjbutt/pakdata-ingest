"""easydata_sync — pull configured series from the SBP EasyData API.

EasyData is the free public backbone for Pakistan economic/banking/payments data
(easydata.sbp.org.pk). The series to sync are declared in
ingestion/easydata_series.json (expand with ingestion/discover_easydata.py) and
mapped to PakData catalog ids.

Design (fixes #5, #6):
  - Per-series pipeline: fetch one series -> archive -> parse -> validate ->
    upsert, committing after each. A crash mid-run loses only the in-flight
    series, and a restart skips series already up to date.
  - Quota gate: a persisted QuotaCounter enforces the 2000/day, 250/hour key
    limits across restarts; when the daily budget is exhausted the run stops
    cleanly and is marked 'partial' (not failed).
  - Cheap incremental: instead of re-pulling every series, group by parent
    dataset and call dataset/{code}/meta ONCE per dataset to read each series'
    Last Refresh Date; only series that advanced get a data call.
  - --limit N caps series per run for controlled backfills.

The API key is read from EASYDATA_API_KEY (never stored in the repo); it is
redacted from any error text before it is persisted or alerted.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import quote

from app import db
from ingestion import alerting, storage
from ingestion.discover_easydata import parse_dataset_meta
from ingestion.easydata_config import EasyDataSeries, by_easydata_key, load_series
from ingestion.framework import IngestionJob, Record
from ingestion.quota import QuotaCounter

API_BASE = "https://easydata.sbp.org.pk/api/v1/series"
DATASET_META_URL = "https://easydata.sbp.org.pk/api/v1/dataset/{code}/meta"
INCREMENTAL_OVERLAP_DAYS = 40
CALL_SPACING_SECONDS = 0.4
DEFAULT_QUOTA_PATH = os.getenv("EASYDATA_QUOTA_PATH", "./data/easydata_quota.json")


# ---- pure parsing (unchanged) -----------------------------------------------

def _col_index(columns: list[str], name: str) -> int:
    for i, c in enumerate(columns):
        if c.strip().lower() == name.strip().lower():
            return i
    raise ValueError(f"EasyData response missing column {name!r}; got {columns}")


def _parse_value(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if s == "" or s.upper() in {"NA", "N/A", "-"}:
        return None
    return float(s)


def _parse_date(raw: str) -> date:
    return datetime.strptime(str(raw).strip()[:10], "%Y-%m-%d").date()


def parse_easydata_json(text: str) -> list[Record]:
    """Parse an EasyData series-data JSON payload into catalog-mapped records.
    Rows are mapped to catalog ids via the 'Series Key' column; unknown keys are
    skipped. Pure — no DB/network."""
    payload = json.loads(text)
    columns = payload["columns"]
    rows = payload.get("rows", [])
    key_idx = _col_index(columns, "Series Key")
    date_idx = _col_index(columns, "Observation Date")
    val_idx = _col_index(columns, "Observation Value")

    mapping = by_easydata_key()
    records: list[Record] = []
    for row in rows:
        cfg = mapping.get(str(row[key_idx]).strip())
        if cfg is None:
            continue
        records.append(
            Record(cfg.id, _parse_date(row[date_idx]), _parse_value(row[val_idx]), {})
        )
    return records


class EasyDataSyncJob(IngestionJob):
    name = "easydata_sync"
    source = "SBP-EasyData"

    def __init__(self, quota_path: str | None = DEFAULT_QUOTA_PATH) -> None:
        self.api_key = os.getenv("EASYDATA_API_KEY")
        self.quota_path = quota_path

    # ---- helpers ------------------------------------------------------------

    def _start_date(self, s: EasyDataSeries, backfill: bool) -> str:
        if backfill:
            return s.backfill_start
        row = db.query_one(
            "SELECT max(obs_date) AS d FROM observations WHERE series_id = %s", (s.id,)
        )
        last = row["d"] if row else None
        return s.backfill_start if last is None else (last - timedelta(days=INCREMENTAL_OVERLAP_DAYS)).isoformat()

    # Per-frequency freshness grace: a series with observations reaching within
    # this many days of today is treated as already fully backfilled (one backfill
    # call pulls the whole history, so "recent data present" == "complete").
    _BACKFILL_GRACE_DAYS = {
        "daily": 7, "weekly": 16, "monthly": 45, "quarterly": 130,
        "annual": 400, "irregular": 60,
    }

    def _select_backfill(self, series: list[EasyDataSeries]) -> list[EasyDataSeries]:
        """Pick series a backfill still needs, skipping ones already fresh. One bulk
        query, not N. **Never-fetched series are returned first**, so a rate-limited
        or restarted backfill spends its scarce quota acquiring data we don't have
        yet rather than re-pulling stale-but-present series."""
        rows = db.query(
            "SELECT series_id, max(obs_date) AS d FROM observations "
            "WHERE series_id = ANY(%(ids)s) GROUP BY series_id",
            {"ids": [s.id for s in series]},
        )
        latest = {r["series_id"]: r["d"] for r in rows}
        today = date.today()
        no_data: list[EasyDataSeries] = []
        stale_present: list[EasyDataSeries] = []
        for s in series:
            d = latest.get(s.id)
            if d is None:
                no_data.append(s)  # highest priority: we have nothing for this series
                continue
            grace = self._BACKFILL_GRACE_DAYS.get(s.frequency, 60)
            if (today - d).days <= grace:
                continue  # already up to date; nothing a backfill would add
            stale_present.append(s)
        return no_data + stale_present

    def _data_url(self, s: EasyDataSeries, backfill: bool) -> str:
        start = self._start_date(s, backfill)
        # Some series codes carry reserved characters (e.g. 'S&NWALR0010'); the
        # key is one path segment and must be percent-encoded or EasyData 500s.
        key = quote(s.easydata_key, safe="")
        return (
            f"{API_BASE}/{key}/data?api_key={self.api_key}"
            f"&start_date={start}&end_date={date.today().isoformat()}&format=json"
        )

    def _fetch_dataset_meta(self, code: str, quota: QuotaCounter) -> dict[str, str | None]:
        """dataset/{code}/meta -> {series_key: last_refresh_date}. One call covers
        every series in the dataset."""
        quota.record()
        url = DATASET_META_URL.format(code=code) + f"?api_key={self.api_key}&format=json"
        content = self.http_get(url)
        entries = parse_dataset_meta(content.decode("utf-8", errors="replace"), code)
        # Pace the meta scan too (not just data pulls) — 200+ back-to-back meta
        # calls otherwise burst past EasyData's rate limit and get 429'd.
        time.sleep(CALL_SPACING_SECONDS)
        return {e["easydata_key"]: e["easydata_last_refresh"] for e in entries}

    def _update_refresh(self, series_id: str, refresh: str | None) -> None:
        if refresh:
            db.execute("UPDATE series SET easydata_last_refresh=%s WHERE id=%s", (refresh, series_id))

    def _select_incremental(
        self, series: list[EasyDataSeries], quota: QuotaCounter
    ) -> tuple[list[EasyDataSeries], dict[str, str | None]]:
        """Pick only series whose source Last Refresh Date advanced. Series without
        a known dataset code can't be cheap-checked and are always included."""
        to_pull: list[EasyDataSeries] = []
        refresh_map: dict[str, str | None] = {}
        by_code: dict[str, list[EasyDataSeries]] = defaultdict(list)
        for s in series:
            (by_code[s.easydata_dataset_code].append(s)
             if s.easydata_dataset_code else to_pull.append(s))
        for code, members in by_code.items():
            if not quota.allow():
                break  # out of budget for meta checks; pull what we already have
            meta = self._fetch_dataset_meta(code, quota)
            for s in members:
                latest = meta.get(s.easydata_key)
                refresh_map[s.easydata_key] = latest
                stored = s.easydata_last_refresh
                if latest is None or stored is None or str(latest) > str(stored):
                    to_pull.append(s)
        return to_pull, refresh_map

    def _pull_one(
        self, s: EasyDataSeries, quota: QuotaCounter, backfill: bool, latest_refresh: str | None
    ) -> int:
        """Fetch + archive + parse + validate + upsert a single series."""
        quota.record()
        content = self.http_get(self._data_url(s, backfill))
        storage.archive(self.source, self.name, date.today(), f"{s.id}.json", content)
        records = parse_easydata_json(content.decode("utf-8", errors="replace"))
        n = self.upsert(self.validate(records))
        self._update_refresh(s.id, latest_refresh)
        time.sleep(CALL_SPACING_SECONDS)
        return n

    def _process_series(
        self,
        series_list: list[EasyDataSeries],
        quota: QuotaCounter,
        process_one: Callable[[EasyDataSeries], int],
    ) -> tuple[str, int, int]:
        """Loop series honoring the quota gate. Returns (status, rows, processed).
        Pure control flow — process_one does the IO — so it is unit-testable.

        One series failing (a source 500, a bad code, a transient DNS flap) must
        never abort a multi-thousand-series backfill: it is logged and skipped, so
        the run keeps going and the failed series is simply retried next run."""
        total = processed = failed = 0
        partial = False
        for s in series_list:
            if not quota.allow():
                partial = True
                break
            try:
                total += process_one(s)
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logging.getLogger("pakdata.ingest").warning(
                    "%s: skipping series %s (%s)", self.name, getattr(s, "id", s),
                    self.redact(str(exc)),
                )
        if failed and not processed:
            status = "failed" if not partial else "partial"
        elif partial:
            status = "partial"
        elif processed:
            status = "success"
        else:
            status = "no_new_data"
        return status, total, processed

    # ---- orchestration ------------------------------------------------------

    def run(self, backfill: bool = False, limit: int | None = None) -> dict:
        run_id = self._start_run()
        try:
            if not self.api_key:
                raise RuntimeError("EASYDATA_API_KEY is not set")
            quota = QuotaCounter(self.quota_path)
            series = load_series()

            if backfill:
                selected, refresh_map = self._select_backfill(series), {}
            else:
                selected, refresh_map = self._select_incremental(series, quota)
            if limit is not None:
                selected = selected[:limit]

            def process_one(s: EasyDataSeries) -> int:
                return self._pull_one(s, quota, backfill, refresh_map.get(s.easydata_key))

            status, total, processed = self._process_series(selected, quota, process_one)
            self._finish(run_id, status, total, None, None)
            if status == "partial":
                alerting.alert(
                    f"{self.name}: stopped at quota",
                    f"processed {processed} series, upserted {total} rows before hitting the daily cap",
                )
            return {"status": status, "rows": total, "processed": processed}
        except Exception as exc:
            msg = self.redact(str(exc))
            self._finish(run_id, "failed", 0, msg, None)
            alerting.alert(f"{self.name}: job failed", msg)
            raise

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "***REDACTED***") if self.api_key else text
