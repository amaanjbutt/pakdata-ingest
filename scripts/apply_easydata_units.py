"""Write the verified dataset scales (ingestion/easydata_units.py) to `series.unit`, and bring the spliced
history (ingestion/easydata_splices.csv) to the unit of the series that holds it.

    python -m scripts.apply_easydata_units            # dry run: what would change
    python -m scripts.apply_easydata_units --commit   # back up, then update

Units: only series still on a bare `usd` / `pkr` unit are touched (PER_UNIT series never), so a re-run is
a no-op; the old units go to `series_unit_bak_<yyyymmdd>` (id, unit).
Splices: a point is rescaled only while it still holds the value listed in the csv, so a re-run is a
no-op; the old values go to `obs_splice_bak_<yyyymmdd>`. This corrects our own merge, not an SBP revision,
so the rows the revision trigger writes for it are deleted in the same transaction (docs/rules.md, 3).
Afterwards bust the catalog cache: `redis-cli --scan --pattern 'cache:v1:*catalog*'` + DEL, and
`DEL cache:v1:status`.
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from decimal import Decimal

from app import db
from ingestion.easydata_units import (
    BARE, PER_UNIT, PER_UNIT_DATASETS, SPLICED, UNVERIFIED, VERIFIED, load_splices, scaled_unit,
)


def plan_units(rows: list[dict]) -> list[tuple[str, str, str]]:
    """Bare series rows (id, unit, easydata_dataset_code) -> (id, old unit, new unit) to write."""
    out = []
    for r in rows:
        new = scaled_unit(r["easydata_dataset_code"], r["id"], r["unit"])
        if new != r["unit"]:
            out.append((r["id"], r["unit"], new))
    return out


def plan_splices(splices: list[dict], current: dict[tuple[str, str], Decimal | None]) -> list[tuple]:
    """csv rows + the values held now -> (series_id, obs_date, old, new) for points still unscaled."""
    out = []
    for s in splices:
        held = current.get((s["series_id"], s["obs_date"]))
        old = Decimal(s["value"])
        if held is None or Decimal(held) != old:
            continue  # already rescaled, revised since, or gone
        factor = Decimal(repr(SPLICED[(s["dataset"], s["merged_from"])]))
        out.append((s["series_id"], s["obs_date"], old, old * factor))
    return out


def main() -> None:
    commit = "--commit" in sys.argv[1:]
    bare = db.query(
        "SELECT id, unit, easydata_dataset_code FROM series WHERE is_active AND unit = ANY(%s) ORDER BY id",
        (list(BARE),),
    )
    units = plan_units(bare)
    code_of = {r["id"]: r["easydata_dataset_code"] for r in bare}
    for code, n in sorted(Counter(code_of[i] for i, _, _ in units).items()):
        print(f"{code}: {n} series -> {VERIFIED[code][0]}")

    splices = load_splices()
    held = db.query(
        "SELECT o.series_id, o.obs_date::text AS d, o.value FROM observations o "
        "JOIN unnest(%s::text[], %s::date[]) AS p(sid, d) ON o.series_id = p.sid AND o.obs_date = p.d "
        "WHERE o.dims = '{}'::jsonb",
        ([s["series_id"] for s in splices], [s["obs_date"] for s in splices]),
    )
    points = plan_splices(splices, {(r["series_id"], r["d"]): r["value"] for r in held})
    todo = {(p[0], p[1]) for p in points}
    pairs = Counter((s["dataset"], s["merged_from"]) for s in splices if (s["series_id"], s["obs_date"]) in todo)
    for (code, src), n in sorted(pairs.items()):
        print(f"splice {code} <- {src}: {n} points x{SPLICED[(code, src)]}")

    changed = {i for i, _, _ in units}
    left = Counter("per-unit" if r["id"] in PER_UNIT or r["easydata_dataset_code"] in PER_UNIT_DATASETS
                   else "unverified" if r["easydata_dataset_code"] in UNVERIFIED
                   else "not EasyData" if not r["easydata_dataset_code"] else "UNSETTLED"
                   for r in bare if r["id"] not in changed)
    print(f"bare afterwards: {dict(sorted(left.items()))}")
    if not units and not points:
        print("nothing to change")
        return
    if not commit:
        print(f"dry run: {len(units)} units and {len(points)} spliced points would change (pass --commit)")
        return

    day = f"{date.today():%Y%m%d}"
    with db.connection() as conn, conn.cursor() as cur:
        if units:
            cur.execute(f"CREATE TABLE IF NOT EXISTS series_unit_bak_{day} (id text PRIMARY KEY, unit text)")
            cur.executemany(f"INSERT INTO series_unit_bak_{day} VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                            [(i, old) for i, old, _ in units])
            cur.executemany("UPDATE series SET unit = %s WHERE id = %s AND unit = %s",
                            [(new, i, old) for i, old, new in units])
        if points:
            cur.execute(f"""CREATE TABLE IF NOT EXISTS obs_splice_bak_{day} (series_id text, obs_date date,
                            dims jsonb, value numeric, PRIMARY KEY (series_id, obs_date, dims))""")
            cur.executemany(f"INSERT INTO obs_splice_bak_{day} VALUES (%s, %s, '{{}}'::jsonb, %s) "
                            "ON CONFLICT DO NOTHING", [(s, d, old) for s, d, old, _ in points])
            cur.executemany("UPDATE observations SET value = %s WHERE series_id = %s AND obs_date = %s "
                            "AND dims = '{}'::jsonb AND value = %s",
                            [(new, s, d, old) for s, d, old, new in points])
            # now() is this transaction's start time — exactly the trigger's superseded_at for these rows
            cur.execute("""DELETE FROM observation_revisions r
                           USING unnest(%s::text[], %s::date[]) AS p(sid, d)
                           WHERE r.series_id = p.sid AND r.obs_date = p.d AND r.dims = '{}'::jsonb
                             AND r.superseded_at = now()""",
                        ([s for s, _, _, _ in points], [d for _, d, _, _ in points]))
            print(f"removed {cur.rowcount} trigger revision rows (our fix, not an SBP revision)")
    print(f"updated {len(units)} units (old in series_unit_bak_{day}) and {len(points)} spliced points "
          f"(old in obs_splice_bak_{day})")


if __name__ == "__main__":
    main()
