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
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from typing import Callable
from urllib.parse import quote

from app import db
from ingestion import alerting, storage
from ingestion.discover_easydata import parse_dataset_meta
from ingestion.easydata_config import EasyDataSeries, by_easydata_key, load_series
from ingestion.framework import IngestionJob, Record
from ingestion.quota import QuotaCounter, QuotaPool

API_BASE = "https://easydata.sbp.org.pk/api/v1/series"
DATASET_META_URL = "https://easydata.sbp.org.pk/api/v1/dataset/{code}/meta"
INCREMENTAL_OVERLAP_DAYS = 40
CALL_SPACING_SECONDS = 0.4
DEFAULT_QUOTA_PATH = os.getenv("EASYDATA_QUOTA_PATH", "./data/easydata_quota.json")

# Each EasyData call is ~3s (slow upstream), and a run is hour-capped at ~675 calls,
# so a SERIAL run takes ~35 min — the binding constraint is wall-clock/CI-minutes, not
# the daily quota (we use ~22% of it). Fetching a few series in parallel cuts a run to
# minutes, so we can run more often and actually spend the quota. Default 1 = the proven
# serial path; the workflow opts into >1. The per-key rate cap is still enforced (the
# quota gate is serialized under a lock), so concurrency never exceeds the rate limit —
# it just stops idling between calls.
EASYDATA_CONCURRENCY = max(1, int(os.getenv("EASYDATA_CONCURRENCY", "1")))

# Wall-clock budget for one run. If EasyData's server is slow/erroring (503s + 30s
# timeouts, which it periodically is), the ~226-dataset meta sweep can grind for hours
# and blow past the GHA job timeout — the run then gets KILLED mid-sweep, never writes a
# terminal status, and leaves a zombie 'running' row that stuck_run_check reaps 12h later
# (with an alert). This deadline makes the run stop CLEANLY as 'partial' well before the
# runner timeout, so it always finishes with a terminal status. Default 20 min (GHA job
# timeout is 30/60 min → comfortable headroom).
RUN_BUDGET_SECONDS = int(os.getenv("EASYDATA_RUN_BUDGET_SECONDS", "1200"))

# Flagship-first: a run is quota-/time-capped and usually stops 'partial' long
# before the ~22.6k-series backlog is drained. Pull the economically-visible
# modules FIRST so forex/monetary/CPI (the series users and the landing tape see)
# refresh every run, even while the long tail drains behind them over days. Lower
# number = higher priority; everything unlisted sorts last (9).
_MODULE_PRIORITY = {
    # The visible flagships first, then the modules that are currently stale AND still
    # have a source backlog to pull (debt/social/public-finance) — ahead of external's
    # huge long-tail, whose module is already fresh (its newest series are current; the
    # ~10k behind ones are low-value deep history). This drains the stale-module
    # backlogs (small: ~1.4k series) within a run or two instead of weeks behind external.
    "forex": 0, "monetary": 1, "prices": 2,
    "debt": 3, "social": 4, "public-finance": 5,
    "external": 6, "real": 7,
}


def _module_priority(s: EasyDataSeries) -> int:
    return _MODULE_PRIORITY.get(s.module, 9)


def _load_api_keys() -> list[str]:
    """EasyData API keys in rotation order. Prefer EASYDATA_API_KEYS (comma- or
    whitespace-separated) for multi-key rotation; fall back to the single
    EASYDATA_API_KEY. Blanks and duplicates removed, order preserved."""
    raw = os.getenv("EASYDATA_API_KEYS") or os.getenv("EASYDATA_API_KEY") or ""
    seen: set[str] = set()
    keys: list[str] = []
    for k in re.split(r"[,\s]+", raw.strip()):
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


class QuotaExhausted(Exception):
    """Every EasyData key is rate-limited (429) or out of budget. Raised so the
    run stops cleanly and is marked 'partial' — never crashing the whole job (which
    used to happen when a 429 during the dataset meta-check sweep went uncaught)."""


def _is_rate_limited(exc: Exception) -> bool:
    """True for an EasyData HTTP 429 (curl_cffi raises 'HTTP Error 429: ')."""
    return "429" in str(exc)


def _is_unauthorized(exc: Exception) -> bool:
    """True for an EasyData HTTP 401 — an expired or invalid API key. When this
    happens ingestion silently degrades to 0 rows, so we surface it as an alert."""
    return "401" in str(exc)


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
    source = "SBP"

    def __init__(self, quota_path: str | None = DEFAULT_QUOTA_PATH) -> None:
        self.api_keys = _load_api_keys()
        # First key is the default/fallback for single-counter paths and redaction.
        self.api_key = self.api_keys[0] if self.api_keys else None
        self.quota_path = quota_path
        # Dedup the "API key rejected (401)" alert to once per run (a fresh
        # process/run resets it) so an expired key can't email per series.
        self._alerted_401 = False

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

    def _data_url(self, s: EasyDataSeries, backfill: bool, api_key: str | None = None) -> str:
        start = self._start_date(s, backfill)
        # Some series codes carry reserved characters (e.g. 'S&NWALR0010'); the
        # key is one path segment and must be percent-encoded or EasyData 500s.
        key = quote(s.easydata_key, safe="")
        return (
            f"{API_BASE}/{key}/data?api_key={api_key or self.api_key}"
            f"&start_date={start}&end_date={date.today().isoformat()}&format=json"
        )

    def _http_get_rotating(self, quota, build_url: Callable[[str], str]) -> bytes:
        """GET build_url(active_key), rotating keys on a 429. On a rate-limit the
        active key is penalized (skipped until its cooldown) and the call retried on
        the next key that still has budget. Raises QuotaExhausted when no key can
        serve it, so the caller stops cleanly (partial) instead of crashing.

        Non-429 errors propagate unchanged (the per-series/per-dataset handlers skip
        them). Each attempt is one recorded quota call."""
        last_exc: Exception | None = None
        for _ in range(max(1, len(self.api_keys))):
            key = getattr(quota, "api_key", None) or self.api_key
            quota.record()
            try:
                return self.http_get(build_url(key))
            except Exception as exc:  # noqa: BLE001
                if _is_unauthorized(exc) and not self._alerted_401:
                    self._alerted_401 = True
                    alerting.alert(
                        f"{self.name}: EasyData API key rejected (401)",
                        "An EasyData API key returned 401 — it is expired or invalid, "
                        "so EasyData ingestion is degrading. Mint a replacement key and "
                        "update EASYDATA_API_KEYS in BOTH GHA repos (amaanjbutt + amaanu01) "
                        "and the VPS .env. " + self.redact(str(exc)),
                    )
                if not _is_rate_limited(exc):
                    raise
                last_exc = exc
                penalize = getattr(quota, "penalize", None)
                if penalize:
                    penalize()  # retire this key for the hour; rotate below
                if not quota.allow():  # no other key has budget -> stop cleanly
                    raise QuotaExhausted(self.redact(str(exc))) from exc
        raise QuotaExhausted(self.redact(str(last_exc)) if last_exc else "quota exhausted")

    def _fetch_dataset_meta(self, code: str, quota) -> dict[str, str | None]:
        """dataset/{code}/meta -> {series_key: last_refresh_date}. One call covers
        every series in the dataset. Rotates keys on a 429."""
        content = self._http_get_rotating(
            quota, lambda key: DATASET_META_URL.format(code=code) + f"?api_key={key}&format=json"
        )
        entries = parse_dataset_meta(content.decode("utf-8", errors="replace"), code)
        # Pace the meta scan too (not just data pulls) — 200+ back-to-back meta
        # calls otherwise burst past EasyData's rate limit and get 429'd.
        time.sleep(CALL_SPACING_SECONDS)
        return {e["easydata_key"]: e["easydata_last_refresh"] for e in entries}

    def _update_refresh(self, series_id: str, refresh: str | None) -> None:
        if refresh:
            db.execute("UPDATE series SET easydata_last_refresh=%s WHERE id=%s", (refresh, series_id))

    def _select_incremental(
        self, series: list[EasyDataSeries], quota: QuotaCounter,
        deadline: float | None = None,
    ) -> tuple[list[EasyDataSeries], dict[str, str | None]]:
        """Pick only series whose source Last Refresh Date advanced. Series without
        a known dataset code can't be cheap-checked and are always included.

        Stops the meta sweep at `deadline` (wall-clock) so a slow/erroring EasyData
        server can't hang the whole run — we pull whatever advanced datasets we found."""
        to_pull: list[EasyDataSeries] = []
        refresh_map: dict[str, str | None] = {}
        by_code: dict[str, list[EasyDataSeries]] = defaultdict(list)
        for s in series:
            (by_code[s.easydata_dataset_code].append(s)
             if s.easydata_dataset_code else to_pull.append(s))
        # Meta-check flagship datasets first, so their series enter `to_pull` even if
        # the sweep is cut short by the quota gate.
        ordered = sorted(by_code.items(),
                         key=lambda kv: min(_module_priority(s) for s in kv[1]))
        for code, members in ordered:
            if deadline is not None and time.time() > deadline:
                logging.getLogger("pakdata.ingest").warning(
                    "%s: meta sweep hit time budget — pulling what advanced so far", self.name)
                break  # slow/erroring source; don't hang past the runner timeout
            if not quota.allow():
                break  # out of budget for meta checks; pull what we already have
            try:
                meta = self._fetch_dataset_meta(code, quota)
            except QuotaExhausted:
                break  # every key is 429'd — stop the sweep, pull what we found
            except Exception as exc:  # one dataset's meta failing must not abort the sweep
                logging.getLogger("pakdata.ingest").warning(
                    "%s: meta check failed for dataset %s (%s)",
                    self.name, code, self.redact(str(exc)),
                )
                continue
            for s in members:
                latest = meta.get(s.easydata_key)
                refresh_map[s.easydata_key] = latest
                stored = s.easydata_last_refresh
                if latest is None or stored is None or str(latest) > str(stored):
                    to_pull.append(s)
        # Flagship modules (forex/monetary/CPI) pulled before the quota cap is hit.
        to_pull.sort(key=_module_priority)  # stable: preserves order within a priority
        return to_pull, refresh_map

    def _pull_one(
        self, s: EasyDataSeries, quota: QuotaCounter, backfill: bool, latest_refresh: str | None
    ) -> int:
        """Fetch + archive + parse + validate + upsert a single series. Rotates keys on a 429."""
        content = self._http_get_rotating(quota, lambda key: self._data_url(s, backfill, key))
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
        deadline: float | None = None,
    ) -> tuple[str, int, int]:
        """Loop series honoring the quota gate. Returns (status, rows, processed).
        Pure control flow — process_one does the IO — so it is unit-testable.

        One series failing (a source 500, a bad code, a transient DNS flap) must
        never abort a multi-thousand-series backfill: it is logged and skipped, so
        the run keeps going and the failed series is simply retried next run."""
        total = processed = failed = 0
        partial = False
        for s in series_list:
            if deadline is not None and time.time() > deadline:
                partial = True
                break  # stop cleanly at the time budget (don't hang past runner timeout)
            if not quota.allow():
                partial = True
                break
            try:
                total += process_one(s)
                processed += 1
            except QuotaExhausted:
                partial = True
                break  # every key is 429'd — stop cleanly and resume next run
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

    def _process_series_concurrent(
        self,
        series_list: list[EasyDataSeries],
        quota,
        backfill: bool,
        refresh_map: dict[str, str | None],
        workers: int,
        deadline: float | None = None,
    ) -> tuple[str, int, int]:
        """Same contract as _process_series, but fetches up to `workers` series in
        parallel. The slow part is the ~3s HTTP round-trip; parse/upsert are fast and
        the psycopg pool (max_size=10) is thread-safe, so only the quota bookkeeping
        needs a lock. The per-key rate cap is therefore still honoured — concurrency
        just removes the idle time between sequential calls.

        Relies on EASYDATA_QUOTA_NOSLEEP (set on the runner) so a capped quota returns
        False instead of sleeping while holding the lock."""
        qlock = threading.Lock()
        stop = threading.Event()
        agg = {"rows": 0, "processed": 0, "failed": 0, "stopped": False}
        alock = threading.Lock()
        log = logging.getLogger("pakdata.ingest")

        def claim_key(rotate: bool = False) -> str | None:
            """Atomically take one quota slot and return the key to use (None = no
            budget). `rotate` first penalizes the current key after a 429."""
            with qlock:
                if rotate:
                    penalize = getattr(quota, "penalize", None)
                    if penalize:
                        penalize()
                if not quota.allow():
                    return None
                key = getattr(quota, "api_key", None) or self.api_key
                quota.record()
                return key

        def work(s: EasyDataSeries) -> None:
            if stop.is_set():
                return
            key = claim_key()
            if key is None:
                stop.set(); agg["stopped"] = True
                return
            content = None
            for _ in range(max(1, len(self.api_keys))):
                try:
                    content = self.http_get(self._data_url(s, backfill, key))
                    break
                except Exception as exc:  # noqa: BLE001
                    if not _is_rate_limited(exc):
                        with alock:
                            agg["failed"] += 1
                        log.warning("%s: skipping series %s (%s)", self.name, s.id, self.redact(str(exc)))
                        return
                    key = claim_key(rotate=True)  # 429 → penalize + rotate to another key
                    if key is None:
                        stop.set(); agg["stopped"] = True
                        return
            if content is None:
                return
            try:
                storage.archive(self.source, self.name, date.today(), f"{s.id}.json", content)
                records = parse_easydata_json(content.decode("utf-8", errors="replace"))
                n = self.upsert(self.validate(records))
                self._update_refresh(s.id, refresh_map.get(s.easydata_key))
            except Exception as exc:  # noqa: BLE001 — one bad series must not abort the run
                with alock:
                    agg["failed"] += 1
                log.warning("%s: skipping series %s (%s)", self.name, s.id, self.redact(str(exc)))
                return
            with alock:
                agg["rows"] += n
                agg["processed"] += 1

        with ThreadPoolExecutor(max_workers=workers) as ex:
            inflight: set = set()
            for s in series_list:
                if stop.is_set():
                    break
                if deadline is not None and time.time() > deadline:
                    agg["stopped"] = True  # time budget hit → stop cleanly as partial
                    break
                inflight.add(ex.submit(work, s))
                if len(inflight) >= workers * 2:  # bounded queue: don't submit all 22k at once
                    _, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            wait(inflight)

        if agg["failed"] and not agg["processed"]:
            status = "partial" if agg["stopped"] else "failed"
        elif agg["stopped"]:
            status = "partial"
        elif agg["processed"]:
            status = "success"
        else:
            status = "no_new_data"
        return status, agg["rows"], agg["processed"]

    # ---- orchestration ------------------------------------------------------

    def run(self, backfill: bool = False, limit: int | None = None) -> dict:
        run_id = self._start_run()
        try:
            if not self.api_keys:
                raise RuntimeError("EASYDATA_API_KEY(S) is not set")
            quota = (
                QuotaPool(self.api_keys, base_path=self.quota_path)
                if len(self.api_keys) > 1
                else QuotaCounter(self.quota_path)
            )
            series = load_series()
            # Wall-clock deadline so the run always finishes with a terminal status
            # (partial) even if EasyData is slow/erroring — never hangs to the runner
            # timeout and leaves a zombie 'running' row.
            deadline = time.time() + RUN_BUDGET_SECONDS

            if backfill:
                selected, refresh_map = self._select_backfill(series), {}
            else:
                selected, refresh_map = self._select_incremental(series, quota, deadline)
            if limit is not None:
                selected = selected[:limit]

            if EASYDATA_CONCURRENCY > 1:
                status, total, processed = self._process_series_concurrent(
                    selected, quota, backfill, refresh_map, EASYDATA_CONCURRENCY, deadline
                )
            else:
                def process_one(s: EasyDataSeries) -> int:
                    return self._pull_one(s, quota, backfill, refresh_map.get(s.easydata_key))

                status, total, processed = self._process_series(selected, quota, process_one, deadline)
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
        for k in self.api_keys:
            text = text.replace(k, "***REDACTED***")
        return text
