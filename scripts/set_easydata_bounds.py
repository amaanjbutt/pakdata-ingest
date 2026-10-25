"""Learn plausibility bounds for the EasyData macro series (HANDOFF §5.2).

The ~22.9k EasyData series were seeded with NULL min/max_value, so a grossly
corrupt source value (a decimal-point slip, a units mix-up, a sign flip carrying
magnitude) would pass `IngestionJob.validate()` silently. This backfills a
guardrail derived from each series' own history:

    max_value =  MULTIPLIER * max(abs(value))
    min_value = -MULTIPLIER * max(abs(value))

By construction this never rejects data already stored. The multiplier is
deliberately generous (50x) so it catches only order-of-magnitude corruption,
not organic macro growth. It is applied ONLY to EasyData series
(`easydata_dataset_code IS NOT NULL`) — mature macro aggregates — and never to
the alternate-data series (Raast, POS, cards, …), which legitimately grow
explosively from near-zero and have no dataset code.

Safe to re-run: it only fills series where `max_value IS NULL`, so any hand-tuned
bounds are left untouched. Symmetric bounds keep the sign flexibility that
balance/net series (current account, NFA, …) need.

    docker compose exec api python -m scripts.set_easydata_bounds [--dry-run]
"""
from __future__ import annotations

import argparse

from app import db

MULTIPLIER = 50
MIN_OBS = 8  # enough history to characterize a plausible range

_STATS_CTE = """
WITH stats AS (
    SELECT o.series_id,
           max(abs(o.value)) AS m,
           count(*)          AS n
    FROM observations o
    JOIN series s ON s.id = o.series_id
    WHERE s.is_active
      AND s.easydata_dataset_code IS NOT NULL
      AND s.max_value IS NULL
      AND o.value IS NOT NULL
    GROUP BY o.series_id
)
"""


def run(dry_run: bool = False) -> int:
    eligible = db.query_one(
        _STATS_CTE + "SELECT count(*) AS c FROM stats WHERE n >= %(min_obs)s AND m > 0",
        {"min_obs": MIN_OBS},
    )["c"]
    if dry_run:
        print(f"[dry-run] would set bounds on {eligible} EasyData series "
              f"(multiplier={MULTIPLIER}x, min_obs={MIN_OBS})")
        return eligible

    updated = db.execute(
        _STATS_CTE
        + """
        UPDATE series s
        SET max_value =  %(mult)s * stats.m,
            min_value = -%(mult)s * stats.m
        FROM stats
        WHERE s.id = stats.series_id
          AND stats.n >= %(min_obs)s
          AND stats.m > 0
        """,
        {"mult": MULTIPLIER, "min_obs": MIN_OBS},
    )
    print(f"set learned bounds on {updated} EasyData series "
          f"(multiplier={MULTIPLIER}x, min_obs={MIN_OBS})")
    return updated


def main() -> None:
    p = argparse.ArgumentParser(prog="scripts.set_easydata_bounds")
    p.add_argument("--dry-run", action="store_true",
                   help="report how many series would be updated, change nothing")
    args = p.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
