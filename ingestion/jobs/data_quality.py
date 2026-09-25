"""data_quality — nightly sweep that keeps published data plausible.

Ingestion jobs validate what they parse, but some glitches only become visible once a
point has neighbours (an isolated spike needs the next day's value), and some arrive
through paths a parser guard can't see (history backfills, other GHA repos). This job
runs on the VPS after the day's ingestion and applies idempotent, audited rules:

1. **Fund NAVs** — non-positive NAVs and isolated spikes (a point >40% away from both
   neighbours while the neighbours agree within 10%) move to `fund_navs_quarantine`.
2. **Debt-security prices** — floating PIB / GoP Ijara Sukuk prices outside 50-150
   (per 100 face value) move to `security_prices_quarantine`.
3. **Auction cut-offs** — T-Bill/PIB yields outside 0.5-40% are removed from the
   `auctions` table and the `rates.auction.*` series (logged in `auctions_quarantine`).
4. **Unit-scale slips in SBP level series** — a single point published at 1000x or
   1/1000x (or 10^6) of its scale, detected strictly: both neighbours agree within 1.5x,
   are >= 1 unit (tiny lumpy flows can swing 1000x for real), and rescaling by exactly 1000^k lands the point BETWEEN them.
   Percent/index/app-dep series are never touched. The value is corrected in place; the
   original is kept by the observation_revisions trigger and in `obs_scale_corrections`.

Nothing is deleted outright: every removed/changed row is kept in an audit table.
"""
from __future__ import annotations

import logging

from app import db
from ingestion import alerting
from ingestion.framework import IngestionJob

log = logging.getLogger("pakdata.data_quality")

_DDL = [
    """CREATE TABLE IF NOT EXISTS fund_navs_quarantine (LIKE fund_navs INCLUDING DEFAULTS,
         reason TEXT, quarantined_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS security_prices_quarantine (LIKE security_prices INCLUDING DEFAULTS,
         reason TEXT, quarantined_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS auctions_quarantine (LIKE auctions INCLUDING DEFAULTS,
         reason TEXT, quarantined_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS obs_scale_corrections (series_id TEXT, obs_date DATE, dims JSONB,
         old_value NUMERIC, new_value NUMERIC, factor NUMERIC, corrected_at TIMESTAMPTZ DEFAULT now())""",
]

_NAV_SPIKES = """
WITH n AS (
  SELECT fund_id, obs_date, nav, lag(nav) OVER w pv, lead(nav) OVER w nv
  FROM fund_navs WINDOW w AS (PARTITION BY fund_id ORDER BY obs_date))
SELECT fund_id, obs_date FROM n
WHERE nav <= 0
   OR (pv > 0 AND nv > 0 AND nav > 0 AND abs(pv/nv - 1) < 0.10 AND (nav/pv > 1.4 OR nav/pv < 0.6))
"""

_SCALE_SLIPS = """
WITH o AS (
  SELECT o.series_id, o.dims, o.obs_date, o.value,
         lag(o.value) OVER w pv, lead(o.value) OVER w nv
  FROM observations o
  JOIN series s ON s.id = o.series_id
  WHERE s.is_active AND s.easydata_dataset_code IS NOT NULL
    AND coalesce(s.unit, '') NOT IN ('percent', 'index', 'ratio')
    AND s.id NOT LIKE '%%app_dep%%' AND s.name NOT ILIKE '%%percent%%' AND s.name NOT ILIKE '%%growth%%'
  WINDOW w AS (PARTITION BY o.series_id, o.dims ORDER BY o.obs_date)),
c AS (
  SELECT *, power(1000, round(log(abs(value) / sqrt(abs(pv * nv))) / 3)) AS f
  FROM o
  WHERE pv IS NOT NULL AND nv IS NOT NULL AND value <> 0 AND pv <> 0 AND nv <> 0
    AND sign(pv) = sign(nv) AND sign(value) = sign(pv)
    AND least(abs(pv), abs(nv)) >= 1   -- tiny lumpy flows (<1 unit) swing 1000x for real
    AND abs(pv / NULLIF(nv, 0)) BETWEEN 0.67 AND 1.5)
SELECT series_id, dims, obs_date, value, f FROM c
WHERE f <> 1
  AND abs(value / f) BETWEEN least(abs(pv), abs(nv)) * 0.8 AND greatest(abs(pv), abs(nv)) * 1.25
"""


class DataQualityJob(IngestionJob):
    name = "data_quality"
    source = "PakDataHub"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            for ddl in _DDL:
                db.execute(ddl)
            res = {
                "nav": self._quarantine_navs(),
                "securities": self._quarantine_security_prices(),
                "auctions": self._quarantine_auctions(),
                "scale": self._fix_scale_slips(),
            }
            total = sum(res.values())
            self._finish(run_id, "success", total, None, None)
            if total >= 50:  # a normal night is ~0; a burst means a feed changed format
                alerting.alert(f"{self.name}: {total} anomalies handled", str(res))
            log.info("data_quality: %s", res)
            return {"status": "success", "rows": total, **res}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            raise

    def _quarantine_navs(self) -> int:
        rows = db.query(_NAV_SPIKES)
        if not rows:
            return 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """WITH d AS (DELETE FROM fund_navs WHERE fund_id=%s AND obs_date=%s RETURNING *)
                       INSERT INTO fund_navs_quarantine SELECT d.*, 'nonpositive_or_isolated_spike' FROM d""",
                    [(r["fund_id"], r["obs_date"]) for r in rows],
                )
        return len(rows)

    def _quarantine_security_prices(self) -> int:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH d AS (
                         DELETE FROM security_prices sp USING securities s
                         WHERE s.code = sp.code AND s.security_type IN ('pib_floating', 'gis_sukuk')
                           AND (sp.price < 50 OR sp.price > 150)
                         RETURNING sp.*)
                       INSERT INTO security_prices_quarantine SELECT d.*, 'price_out_of_range' FROM d"""
                )
                return cur.rowcount or 0

    def _quarantine_auctions(self) -> int:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """WITH d AS (
                         DELETE FROM auctions WHERE auction_type IN ('tbill', 'pib')
                           AND (cutoff_yield < 0.5 OR cutoff_yield > 40) RETURNING *)
                       INSERT INTO auctions_quarantine SELECT d.*, 'yield_out_of_range' FROM d"""
                )
                n = cur.rowcount or 0
                cur.execute(
                    """DELETE FROM observations WHERE series_id ~ '^rates\\.auction\\.(tbill|pib)\\.'
                         AND (value < 0.5 OR value > 40)"""
                )
        return n

    def _fix_scale_slips(self) -> int:
        rows = db.query(_SCALE_SLIPS)
        if not rows:
            return 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO obs_scale_corrections (series_id, obs_date, dims, old_value, new_value, factor)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    [(r["series_id"], r["obs_date"], _json(r["dims"]), r["value"], r["value"] / r["f"], r["f"])
                     for r in rows],
                )
                cur.executemany(
                    """UPDATE observations SET value = value / %s, revised_at = now()
                       WHERE series_id = %s AND obs_date = %s AND dims = %s::jsonb""",
                    [(r["f"], r["series_id"], r["obs_date"], _json(r["dims"])) for r in rows],
                )
        return len(rows)


def _json(v) -> str:
    import json

    return json.dumps(v or {})
