"""derive_auctions — auction cut-off yields from EasyData (reliable + deep history).

`kibor.asp`'s auction tables stopped updating after the 2026 SBP redesign, so the
scraped `sbp_auctions` job is stuck. EasyData carries the same primary-market cut-off
yields with YEARS of history — datasets `TS_GP_BAM_SIRTBIL_AH` (T-Bills 3/6/12m) and
`TS_GP_BAM_SIRPIBS_AH` (PIBs 2y–30y), ingested as `banking.cut_off_yield_*` via the
reliable EasyData pipeline. This job derives the flagship `rates.auction.{tbill,pib}.*`
series and the dedicated `auctions` table from those observations — a pure DB derivation
(no external fetch), so it refreshes automatically as EasyData ingests new auctions and
backfills the deep history the scrape never had.

GIS / Ijara Sukuk cut-offs are NOT in EasyData, so `sbp_auctions` still covers those;
the two jobs upsert by (type, tenor, date), so they coexist without clobbering.
"""
from __future__ import annotations

import re

from app import db
from ingestion.framework import IngestionJob

# EasyData dataset code -> auction type. (Ijara Sukuk / GIS is not in EasyData.)
_DATASET_TYPE = {"TS_GP_BAM_SIRTBIL_AH": "tbill", "TS_GP_BAM_SIRPIBS_AH": "pib"}
_MIN_YIELD, _MAX_YIELD = 0.5, 40.0
_TENOR_RE = re.compile(r"(\d+)\s*-?\s*(month|year)", re.I)


def tenor_from_name(name: str) -> str | None:
    """'...Cut-off Yield 3-Months' -> '3m'; '...10-Years' -> '10y'. Pure."""
    m = _TENOR_RE.search(name or "")
    if not m:
        return None
    return f"{m.group(1)}{'m' if m.group(2).lower().startswith('month') else 'y'}"


def map_sources(series_rows: list[dict]) -> dict[str, tuple[str, str]]:
    """{easydata_cutoff_series_id: (auction_type, tenor)} from the catalog rows. Pure."""
    out: dict[str, tuple[str, str]] = {}
    for r in series_rows:
        atype = _DATASET_TYPE.get(r.get("easydata_dataset_code"))
        tenor = tenor_from_name(r.get("name", "")) if atype else None
        if atype and tenor:
            out[r["id"]] = (atype, tenor)
    return out


class DeriveAuctionsJob(IngestionJob):
    name = "derive_auctions"
    source = "SBP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            srows = db.query(
                "SELECT id, name, easydata_dataset_code FROM series "
                "WHERE easydata_dataset_code = ANY(%(codes)s)",
                {"codes": list(_DATASET_TYPE)},
            )
            mapping = map_sources(srows)
            if not mapping:
                self._finish(run_id, "no_new_data", 0, None, None)
                return {"status": "no_new_data", "rows": 0}
            obs = db.query(
                "SELECT series_id, obs_date, value FROM observations "
                "WHERE series_id = ANY(%(ids)s) AND value IS NOT NULL",
                {"ids": list(mapping)},
            )
            points: dict[tuple[str, str], list[tuple]] = {}
            for o in obs:
                v = float(o["value"])
                # A T-Bill/PIB cut-off outside 0.5-40% is a source glitch (EasyData has a
                # 6m T-Bill of 0.1215% on 2007-12-05, when the policy rate was ~10%).
                if not (_MIN_YIELD <= v <= _MAX_YIELD):
                    continue
                key = mapping[o["series_id"]]
                points.setdefault(key, []).append((o["obs_date"], v))
            total = 0
            for (atype, tenor), pts in points.items():
                self._ensure_series(atype, tenor)
                total += self._upsert(atype, tenor, pts)
            self._finish(run_id, "success", total, None, None)
            return {"status": "success", "rows": total, "series": len(points)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            raise

    @staticmethod
    def _target_id(atype: str, tenor: str) -> str:
        return f"rates.auction.{atype}.{tenor}.cutoff_yield"

    def _ensure_series(self, atype: str, tenor: str) -> None:
        """Create the flagship auction series if absent (it usually already exists from
        sbp_auctions); leave existing metadata untouched."""
        sid = self._target_id(atype, tenor)
        label = {"tbill": "T-Bill", "pib": "PIB"}[atype]
        db.execute(
            """INSERT INTO series (id, module, name, description, unit, frequency, source, tier, is_active)
               VALUES (%s, 'fixed-income', %s, %s, 'percent', 'irregular', 'SBP', 'basic', true)
               ON CONFLICT (id) DO UPDATE SET is_active = true""",
            (sid, f"{label} Auction Cut-off Yield {tenor.upper()}",
             f"Primary-market {label} auction cut-off yield, {tenor} tenor. "
             f"Source: State Bank of Pakistan (via EasyData), compiled by PakData."),
        )

    def _upsert(self, atype: str, tenor: str, pts: list[tuple]) -> int:
        if not pts:
            return 0
        sid = self._target_id(atype, tenor)
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                       VALUES (%s, %s, %s, '{}'::jsonb, false, now())
                       ON CONFLICT (series_id, obs_date, dims)
                       DO UPDATE SET value = EXCLUDED.value, revised_at = now()""",
                    [(sid, d, v) for d, v in pts],
                )
                cur.executemany(
                    """INSERT INTO auctions (auction_type, tenor, auction_date, cutoff_yield, source)
                       VALUES (%s, %s, %s, %s, 'SBP')
                       ON CONFLICT (auction_type, tenor, auction_date)
                       DO UPDATE SET cutoff_yield = EXCLUDED.cutoff_yield, revised_at = now()""",
                    [(atype, tenor, d, v) for d, v in pts],
                )
                cur.execute(
                    "UPDATE series SET first_date=(SELECT min(obs_date) FROM observations WHERE series_id=%s), "
                    "last_date=(SELECT max(obs_date) FROM observations WHERE series_id=%s) WHERE id=%s",
                    (sid, sid, sid),
                )
        return len(pts)
