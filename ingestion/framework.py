"""Ingestion framework: archive → parse → validate → upsert → track → alert.

Concrete jobs subclass `IngestionJob` and implement `fetch()` and `parse()`.
The base class handles idempotent upserts, validation gates against the series
catalog, ingestion_runs tracking, and alerting.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from app import db
from app.config import settings
from ingestion import alerting, storage

log = logging.getLogger("pakdata.ingest")


@dataclass
class Record:
    """One parsed observation."""

    series_id: str
    obs_date: date
    value: float | None
    dims: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchedFile:
    filename: str
    content: bytes
    when: date


class ValidationError(Exception):
    pass


# Flagship-id canonicalization (2026-07-30 rename). Scraped jobs historically
# emitted short ids (kibor.3m, cpi.national, ...); the public catalog now uses the
# unified `topic.group.item` scheme. Rewriting at the ingestion boundary means a
# job can never persist a stale id even if a constant is missed. Keep in sync with
# the catalog seeders.
def canonicalize_id(sid: str) -> str:
    if sid == "policy_rate" or sid.startswith("policy_rate."):
        return "rates.policy" + sid[len("policy_rate"):]
    if sid == "wpi" or sid.startswith("wpi."):
        return "inflation.wpi" + sid[len("wpi"):]
    for old, new in (
        ("kibor.", "rates.kibor."), ("pkrv.", "rates.pkrv."),
        ("pkisrv.", "rates.pkisrv."), ("auction.", "rates.auction."),
        ("cpi.", "inflation.cpi."), ("lsm.", "industry.lsm."),
        ("commodity.", "commodities."),
    ):
        if sid.startswith(old):
            return new + sid[len(old):]
    return sid


class IngestionJob:
    name: str = "base"
    source: str = "UNKNOWN"
    # Records dropped by the last validate() call (bounds/date gates).
    last_rejects: list[str] = []

    # ---- to be implemented by concrete jobs ----------------------------------

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        """Return raw source files. Concrete jobs override this."""
        raise NotImplementedError

    def parse(self, f: FetchedFile) -> list[Record]:
        """Parse one fetched file into observation records."""
        raise NotImplementedError

    def redact(self, text: str) -> str:
        """Scrub secrets (e.g. API keys) from error text before it is stored or
        alerted. Default is a no-op; jobs with credentials override this."""
        return text

    # ---- HTTP helper ---------------------------------------------------------

    def http_get(self, url: str) -> bytes:
        # Browser-TLS impersonation (curl_cffi) so Cloudflare-fronted official
        # sources don't fingerprint-block us. Low-frequency polling, bounded
        # timeout; retries handled by caller/scheduler.
        from ingestion import http_client

        return http_client.get(url, timeout=30).content

    # ---- validation ----------------------------------------------------------

    def _catalog_bounds(self, series_ids: set[str]) -> dict[str, dict]:
        if not series_ids:
            return {}
        rows = db.query(
            "SELECT id, min_value, max_value, max_step FROM series WHERE id = ANY(%(ids)s)",
            {"ids": list(series_ids)},
        )
        return {r["id"]: r for r in rows}

    def _prev_value(self, series_id: str, dims: dict) -> tuple[float | None, date | None]:
        """The latest stored (value, date) for a series — the baseline the large-step
        gate compares a genuinely-new observation against."""
        row = db.query_one(
            "SELECT value, obs_date FROM observations WHERE series_id=%(sid)s AND dims @> %(dims)s::jsonb "
            "ORDER BY obs_date DESC LIMIT 1",
            {"sid": series_id, "dims": json.dumps(dims)},
        )
        if not row or row["value"] is None:
            return None, None
        return float(row["value"]), row["obs_date"]

    def validate(self, records: list[Record]) -> list[tuple[Record, bool]]:
        """Return (record, flagged) pairs for the records that pass the gates.

        Gates (PRD §5.3): value within catalog bounds, date not in the future,
        large step vs previous observation is written-but-flagged + alerted.

        An out-of-bounds value **rejects that record**, not the whole run: source
        files occasionally carry a typo (e.g. a 42% 10-year yield), and losing an
        entire multi-year backfill to one bad cell is far worse than dropping it.
        Rejects are counted, logged and alerted; if *every* record is rejected
        that's real breakage and we raise.
        """
        for r in records:  # rewrite legacy scraped ids to the canonical scheme
            r.series_id = canonicalize_id(r.series_id)
        bounds = self._catalog_bounds({r.series_id for r in records})
        today = date.today()
        out: list[tuple[Record, bool]] = []
        rejects: list[str] = []
        large_steps: list[str] = []
        for r in records:
            if r.obs_date > today:
                rejects.append(f"{r.series_id}: date in the future {r.obs_date}")
                continue
            b = bounds.get(r.series_id)
            if b is None:
                rejects.append(f"{r.series_id}: not in catalog")
                continue
            if r.value is not None:
                lo, hi = b.get("min_value"), b.get("max_value")
                if lo is not None and r.value < float(lo):
                    rejects.append(f"{r.series_id}: {r.value} < min {lo} on {r.obs_date}")
                    continue
                if hi is not None and r.value > float(hi):
                    rejects.append(f"{r.series_id}: {r.value} > max {hi} on {r.obs_date}")
                    continue
            flagged = False
            step = b.get("max_step")
            if step is not None and r.value is not None:
                prev, prev_date = self._prev_value(r.series_id, r.dims)
                # Only flag a large step for records that EXTEND the series past its
                # current latest date. Comparing RE-INGESTED history against the newest
                # stored value (e.g. 2017 CPI 107 vs 2026 CPI 293) is a false positive,
                # and a backfill/full-history re-parse otherwise fires one alert per
                # historical month.
                if (prev is not None and prev_date is not None
                        and r.obs_date > prev_date and abs(r.value - prev) > float(step)):
                    flagged = True
                    large_steps.append(f"{r.series_id}: {prev} → {r.value} on {r.obs_date}")
            out.append((r, flagged))

        # One summary email per run, not one per flagged record.
        if large_steps:
            log.warning("%s: %d large step(s) flagged (first: %s)",
                        self.name, len(large_steps), large_steps[0])
            more = f"; …+{len(large_steps) - 5} more" if len(large_steps) > 5 else ""
            alerting.alert(
                f"{self.name}: {len(large_steps)} large step(s) flagged",
                "; ".join(large_steps[:5]) + more,
            )

        if rejects:
            if not out:
                raise ValidationError(
                    f"all {len(records)} record(s) rejected; first: {rejects[0]}"
                )
            self.last_rejects = rejects
            log.warning("%s: rejected %d/%d records (first: %s)",
                        self.name, len(rejects), len(records), rejects[0])
            alerting.alert(
                f"{self.name}: {len(rejects)} record(s) failed validation",
                "; ".join(rejects[:5]),
            )
        return out

    # ---- upsert --------------------------------------------------------------

    def upsert(self, validated: list[tuple[Record, bool]]) -> int:
        if not validated:
            return 0
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                for r, flagged in validated:
                    cur.execute(
                        """
                        INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                        VALUES (%s, %s, %s, %s::jsonb, %s, now())
                        ON CONFLICT (series_id, obs_date, dims)
                        DO UPDATE SET value = EXCLUDED.value,
                                      flagged = EXCLUDED.flagged,
                                      revised_at = now()
                        """,
                        (
                            r.series_id,
                            r.obs_date,
                            Decimal(str(r.value)) if r.value is not None else None,
                            json.dumps(r.dims),
                            flagged,
                        ),
                    )
                    n += 1
                # Keep series.first_date/last_date fresh for /status and meta.
                bounds_by_series: dict[str, tuple[date, date]] = {}
                for r, _ in validated:
                    lo, hi = bounds_by_series.get(r.series_id, (r.obs_date, r.obs_date))
                    bounds_by_series[r.series_id] = (
                        min(lo, r.obs_date),
                        max(hi, r.obs_date),
                    )
                for sid, (mind, maxd) in bounds_by_series.items():
                    cur.execute(
                        "UPDATE series SET last_date = GREATEST(COALESCE(last_date, %s), %s), "
                        "first_date = LEAST(COALESCE(first_date, %s), %s) WHERE id = %s",
                        (maxd, maxd, mind, mind, sid),
                    )
        return n

    # ---- orchestration -------------------------------------------------------

    def _start_run(self) -> int:
        return db.query_one(
            "INSERT INTO ingestion_runs (job_name, started_at, status) "
            "VALUES (%s, %s, 'running') RETURNING id",
            (self.name, datetime.now(timezone.utc)),
        )["id"]

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        raw_path: str | None = None
        try:
            files = self.fetch(backfill=backfill)
            if not files:
                self._finish(run_id, "no_new_data", 0, None, None)
                alerting.alert(f"{self.name}: no new data", "Expected data but found none.")
                return {"status": "no_new_data", "rows": 0}

            # Parse each file independently. A multi-file backfill (hundreds of
            # daily files) must not be lost because one issue has a malformed or
            # off-format file — skip it, remember it, and keep going. If *every*
            # file fails, that's a real breakage and we raise.
            all_records: list[Record] = []
            failures: list[str] = []
            for f in files:
                raw_path = storage.archive(self.source, self.name, f.when, f.filename, f.content)
                try:
                    all_records.extend(self.parse(f))
                except Exception as exc:
                    failures.append(f"{f.filename}: {self.redact(str(exc))}")
                    log.warning("%s: skipping unparseable file %s (%s)", self.name, f.filename, exc)

            if failures and not all_records:
                raise ValueError(
                    f"all {len(files)} file(s) failed to parse; first: {failures[0]}"
                )

            validated = self.validate(all_records)
            rows = self.upsert(validated)
            # Fire webhooks on incremental updates only — never during a backfill,
            # which would flood subscribers. Best-effort; never breaks a run.
            if rows and not backfill:
                # Only generic series records carry series_id; dedicated-table jobs
                # (funds, securities, trades, portfolio) have none — skip them.
                series_ids = {
                    sid for r, _ in validated if (sid := getattr(r, "series_id", None))
                }
                if series_ids:
                    try:
                        from app.services.webhook_delivery import deliver_for_series
                        deliver_for_series(series_ids)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s: webhook delivery error: %s", self.name, self.redact(str(exc)))
            if failures:
                msg = f"{len(failures)}/{len(files)} file(s) skipped: {failures[0]}"
                self._finish(run_id, "partial", rows, msg, raw_path)
                alerting.alert(f"{self.name}: some files unparseable", msg)
                log.info("%s: upserted %d rows (%d files skipped)", self.name, rows, len(failures))
                return {"status": "partial", "rows": rows, "skipped": len(failures)}
            self._finish(run_id, "success", rows, None, raw_path)
            log.info("%s: upserted %d rows", self.name, rows)
            return {"status": "success", "rows": rows}
        except Exception as exc:
            msg = self.redact(str(exc))
            self._finish(run_id, "failed", 0, msg, raw_path)
            alerting.alert(f"{self.name}: job failed", msg)
            raise

    def _finish(self, run_id: int, status: str, rows: int, error: str | None, raw: str | None):
        db.execute(
            "UPDATE ingestion_runs SET finished_at=%s, status=%s, rows_upserted=%s, "
            "error=%s, raw_file=%s WHERE id=%s",
            (datetime.now(timezone.utc), status, rows, error, raw, run_id),
        )
