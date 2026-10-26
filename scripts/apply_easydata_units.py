"""Write the verified dataset scales (ingestion/easydata_units.py) to `series.unit`.

    python -m scripts.apply_easydata_units            # dry run: what would change
    python -m scripts.apply_easydata_units --commit   # back up the old units, then update

Only series still on a bare `usd` / `pkr` unit are touched, so a re-run is a no-op. The backup is
`series_unit_bak_<yyyymmdd>` (id, unit). Afterwards bust the catalog cache:
`redis-cli --scan --pattern 'cache:v1:*catalog*'` + DEL, and `DEL cache:v1:status`.
"""
from __future__ import annotations

import sys
from datetime import date

from app import db
from ingestion.easydata_units import BARE, VERIFIED


def main() -> None:
    commit = "--commit" in sys.argv[1:]
    todo = []
    for code, (unit, _source, _checked) in sorted(VERIFIED.items()):
        rows = db.query(
            "SELECT id, unit FROM series WHERE easydata_dataset_code = %s AND unit = ANY(%s) ORDER BY id",
            (code, list(BARE)),
        )
        print(f"{code}: {len(rows)} series {sorted({r['unit'] for r in rows})} -> {unit}")
        todo += [(r["id"], r["unit"], unit) for r in rows]
    if not todo:
        print("nothing to change")
        return
    if not commit:
        print(f"dry run: {len(todo)} series would change (pass --commit)")
        return
    bak = f"series_unit_bak_{date.today():%Y%m%d}"
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {bak} (id text PRIMARY KEY, unit text)")
        cur.executemany(f"INSERT INTO {bak} VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                        [(i, old) for i, old, _ in todo])
        cur.executemany("UPDATE series SET unit = %s WHERE id = %s AND unit = %s",
                        [(new, i, old) for i, old, new in todo])
    print(f"updated {len(todo)} series; old units in {bak}")


if __name__ == "__main__":
    main()
