"""derive_kibor — full KIBOR history in the flagship `rates.kibor.*` ids.

The flagship KIBOR series were fed only by the `sbp_kibor` scrape, so they start on
2026-07-23 (the live kibor.asp page shows today's fixing only). The same fixings, back
to **2005-06-09**, already arrive through the reliable EasyData pipeline as
`banking.<tenor>_karachi_interbank_{bid,offer}` (dataset `TS_GP_BAM_SIRKIBOR_D`). This
job copies them into `rates.kibor.{1w,2w,1m,3m,6m,9m,1y,2y,3y}` with the `side`
dimension — a pure DB derivation (no fetch), like `derive_auctions`.

The scrape stays authoritative for the days it covers: derived points are inserted
with ON CONFLICT DO NOTHING, so a scraped fixing is never overwritten. 2Y and 3Y KIBOR
were discontinued in 2020; those series carry their history and end there.
"""
from __future__ import annotations

import re

from app import db
from ingestion.framework import IngestionJob

# EasyData series name stem -> flagship tenor.
TENORS = {
    "one_week": "1w", "two_weeks": "2w", "one_month": "1m", "three_months": "3m",
    "six_months": "6m", "nine_months": "9m", "one_year": "1y", "two_years": "2y",
    "three_years": "3y",
}
_TENOR_LABEL = {
    "1w": "1-Week", "2w": "2-Week", "1m": "1-Month", "3m": "3-Month", "6m": "6-Month",
    "9m": "9-Month", "1y": "1-Year", "2y": "2-Year", "3y": "3-Year",
}
_SOURCE_RE = re.compile(r"^banking\.([a-z_]+)_karachi_interbank_(bid|offer)$")
_MIN_RATE, _MAX_RATE = 0.5, 40.0


def map_source(series_id: str) -> tuple[str, str] | None:
    """'banking.three_months_karachi_interbank_offer' -> ('3m', 'offer'). Pure."""
    m = _SOURCE_RE.match(series_id)
    if not m or m.group(1) not in TENORS:
        return None
    return TENORS[m.group(1)], m.group(2)


def target_id(tenor: str) -> str:
    return f"rates.kibor.{tenor}"


class DeriveKiborJob(IngestionJob):
    name = "derive_kibor"
    source = "SBP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            sources = {
                r["id"]: map_source(r["id"])
                for r in db.query(
                    "SELECT id FROM series WHERE id LIKE 'banking.%%karachi_interbank_%%'"
                )
            }
            sources = {sid: m for sid, m in sources.items() if m}
            if not sources:
                self._finish(run_id, "no_new_data", 0, None, None)
                return {"status": "no_new_data", "rows": 0}
            obs = db.query(
                "SELECT series_id, obs_date, value FROM observations "
                "WHERE series_id = ANY(%(ids)s) AND value IS NOT NULL",
                {"ids": list(sources)},
            )
            points: dict[str, list[tuple]] = {}
            for o in obs:
                v = float(o["value"])
                if not (_MIN_RATE <= v <= _MAX_RATE):
                    continue
                tenor, side = sources[o["series_id"]]
                points.setdefault(tenor, []).append((o["obs_date"], v, side))
            inserted = 0
            for tenor, pts in points.items():
                self._ensure_series(tenor)
                inserted += self._insert(tenor, pts)
            status = "success" if inserted else "no_new_data"
            self._finish(run_id, status, inserted, None, None)
            return {"status": status, "rows": inserted, "tenors": sorted(points)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            raise

    def _ensure_series(self, tenor: str) -> None:
        """Create/reactivate the flagship tenor; existing metadata is left alone."""
        label = _TENOR_LABEL[tenor]
        db.execute(
            """INSERT INTO series (id, module, name, description, unit, frequency, source,
                                   source_url, dimensions, min_value, max_value, max_step, tier, is_active)
               VALUES (%s, 'fixed-income', %s, %s, 'percent', 'daily', 'SBP',
                       'https://www.sbp.org.pk/ecodata/kibor/kibor.asp', '{"side":["bid","offer"]}',
                       0, 40, 5, 'basic', true)
               ON CONFLICT (id) DO UPDATE SET is_active = true""",
            (target_id(tenor), f"KIBOR {label}",
             f"Karachi Interbank Offered Rate, {label.lower()} tenor (bid/offer), daily from 2005. "
             f"Source: State Bank of Pakistan."),
        )

    def _insert(self, tenor: str, pts: list[tuple]) -> int:
        sid = target_id(tenor)
        with db.connection() as conn:
            with conn.cursor() as cur:
                # DO NOTHING: the same-day scrape (sbp_kibor) wins where both exist.
                cur.execute("SELECT count(*) FROM observations WHERE series_id = %s", (sid,))
                before = cur.fetchone()[0]
                cur.executemany(
                    """INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                       VALUES (%s, %s, %s, jsonb_build_object('side', %s::text), false, now())
                       ON CONFLICT (series_id, obs_date, dims) DO NOTHING""",
                    [(sid, d, v, side) for d, v, side in pts],
                )
                cur.execute("SELECT count(*) FROM observations WHERE series_id = %s", (sid,))
                after = cur.fetchone()[0]
                cur.execute(
                    "UPDATE series SET first_date=(SELECT min(obs_date) FROM observations WHERE series_id=%s), "
                    "last_date=(SELECT max(obs_date) FROM observations WHERE series_id=%s) WHERE id=%s",
                    (sid, sid, sid),
                )
        return after - before
