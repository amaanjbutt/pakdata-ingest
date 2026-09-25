"""cost_of_living — derived per-city cost-of-living index from the weekly SPI.

For each city we rebase every SPI essential-item price to 100 at a base week and
take the unweighted mean of those price relatives — a transparent Jevons-style
index that tracks essential-goods inflation per city. It's derived purely from
data already in `observations` (the pbs_spi_weekly output) — no external fetch —
so it refreshes automatically after each SPI ingest. One series per city:
`cost_of_living.<city>` (module `prices`, weekly, unit `index`). A product only
we compute: PBS publishes per-item city prices but no per-city cost index.
"""
from __future__ import annotations

from collections import defaultdict

from app import db
from ingestion.framework import IngestionJob

# A city needs at least this many base-week items to yield a meaningful index.
_MIN_BASKET = 8


def compute_city_indices(rows: list[dict], min_basket: int = _MIN_BASKET) -> dict:
    """Pure: SPI observation rows -> per-city cost-of-living index.

    `rows` are dicts with `series_id`, `obs_date`, `value`, `city`. Returns
    ``{city: {"base": date, "basket_n": int, "points": [(obs_date, index)]}}``,
    each index rebased to 100 at the earliest week over the fixed base-week
    basket. No DB/network — testable in isolation.
    """
    data: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    all_dates: set = set()
    for r in rows:
        city = r["city"]
        if not city or r["value"] is None:
            continue
        data[city][r["series_id"]][r["obs_date"]] = float(r["value"])
        all_dates.add(r["obs_date"])
    if not all_dates:
        return {}
    dates = sorted(all_dates)
    base = dates[0]
    out: dict = {}
    for city, items in data.items():
        basket = {it: p for it, p in items.items() if p.get(base, 0) > 0}
        if len(basket) < min_basket:
            continue
        points = []
        for w in dates:
            rels = [p[w] / p[base] * 100.0 for p in basket.values() if w in p]
            if rels:
                points.append((w, round(sum(rels) / len(rels), 2)))
        out[city] = {"base": base, "basket_n": len(basket), "points": points}
    return out


class CostOfLivingJob(IngestionJob):
    name = "cost_of_living"
    source = "PBS"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            rows = db.query(
                "SELECT series_id, obs_date, value, dims->>'city' AS city "
                "FROM observations WHERE series_id LIKE 'commodities.%%' AND value IS NOT NULL"
            )
            indices = compute_city_indices(rows)
            total = 0
            for city, info in indices.items():
                sid = f"cost_of_living.{city}"
                self._ensure_series(sid, city, info["base"], info["basket_n"])
                total += self._upsert([(sid, w, v) for w, v in info["points"]])
            self._finish(run_id, "success", total, None, None)
            return {"status": "success", "rows": total, "cities": len(indices)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            raise

    def _ensure_series(self, sid: str, city: str, base, basket_n: int) -> None:
        label = "National" if city == "national" else city.title()
        desc = (
            f"Cost-of-living index for {label}: the unweighted mean of SPI "
            f"essential-item price relatives, rebased to 100 at the week of "
            f"{base.isoformat()} (basket of {basket_n} items). Tracks essential-"
            f"goods inflation for {label}. Source: PBS SPI, computed by PakData."
        )
        db.execute(
            """INSERT INTO series (id, module, name, description, unit, frequency, source, tier, is_active)
               VALUES (%s, 'prices', %s, %s, 'index', 'weekly', 'PBS', 'basic', true)
               ON CONFLICT (id) DO UPDATE SET description = EXCLUDED.description, is_active = true""",
            (sid, f"Cost of Living Index — {label}", desc),
        )

    def _upsert(self, obs: list[tuple]) -> int:
        if not obs:
            return 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                       VALUES (%s, %s, %s, '{}'::jsonb, false, now())
                       ON CONFLICT (series_id, obs_date, dims)
                       DO UPDATE SET value = EXCLUDED.value, revised_at = now()""",
                    obs,
                )
                sid = obs[0][0]
                cur.execute(
                    "UPDATE series SET first_date = (SELECT min(obs_date) FROM observations WHERE series_id=%s), "
                    "last_date = (SELECT max(obs_date) FROM observations WHERE series_id=%s) WHERE id=%s",
                    (sid, sid, sid),
                )
        return len(obs)
