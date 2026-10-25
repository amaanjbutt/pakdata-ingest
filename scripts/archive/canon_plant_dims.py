"""Rewrite stored energy observations to the canonical plant names of ingestion/plants_pk.

    python -m scripts.canon_plant_dims            # dry run: what changes, any collisions
    python -m scripts.canon_plant_dims --commit   # back up, then rewrite in one transaction

`plant` dims are replaced by `canon_plant()`'s output (canonical name + unit / config /
tranche). Rows are deleted and re-inserted with the same value, flags and timestamps, so no
revision is recorded (the revision trigger only fires on UPDATE). If two old spellings land on
the same (series, date, dims) with different values the run stops — the registry is wrong.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict

from app import db
from ingestion.plants_pk import canon_plant, unknown_names

BACKUP = "obs_plant_dims_bak_20261017"


def plan() -> tuple[list[dict], list[str], int]:
    rows = db.query("""SELECT series_id, obs_date, value, dims, flagged, revised_at, first_seen_at
                       FROM observations WHERE series_id LIKE 'energy.%%' AND dims ? 'plant'""")
    out: dict[tuple, dict] = {}
    conflicts: list[str] = []
    changed = 0
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        d = dict(r["dims"])
        raw = d.pop("plant")
        new = {**d, **canon_plant(raw)}
        if new != r["dims"]:
            changed += 1
        groups[(r["series_id"], r["obs_date"], json.dumps(new, sort_keys=True))].append({**r, "new": new, "raw": raw})
    for k, rs in groups.items():
        vals = {None if x["value"] is None else float(x["value"]) for x in rs}
        if len(vals) > 1:
            conflicts.append(f"{k[0]} {k[1]} {k[2]}: " + "; ".join(f"{x['raw']}={x['value']}" for x in rs))
        keep = min(rs, key=lambda x: x["first_seen_at"])        # the earliest-seen copy
        out[k] = keep
    return list(out.values()), conflicts, changed


def main(commit: bool) -> None:
    rows, conflicts, changed = plan()
    total = db.query_one("SELECT count(*) AS n FROM observations WHERE series_id LIKE 'energy.%%' AND dims ? 'plant'")["n"]
    print(f"{total} rows with a plant dim; {changed} change dims; {total - len(rows)} duplicates merge")
    if unknown_names():
        print("NOT IN THE REGISTRY:", unknown_names())
    if conflicts:
        print(f"{len(conflicts)} conflicting merges (stopping):")
        for c in conflicts[:30]:
            print("  ", c)
        sys.exit(1)
    names = sorted({r["new"]["plant"] for r in rows})
    print(f"{len(names)} canonical plants, e.g. {names[:8]}")
    if not commit:
        print("dry run - re-run with --commit")
        return
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {BACKUP} AS SELECT * FROM observations "
                        "WHERE series_id LIKE 'energy.%%' AND dims ? 'plant'")
            cur.execute("DELETE FROM observations WHERE series_id LIKE 'energy.%%' AND dims ? 'plant'")
            deleted = cur.rowcount
            with cur.copy("COPY observations (series_id, obs_date, value, dims, flagged, revised_at, first_seen_at) "
                          "FROM STDIN") as cp:
                for r in rows:
                    cp.write_row((r["series_id"], r["obs_date"], r["value"], json.dumps(r["new"]),
                                  r["flagged"], r["revised_at"], r["first_seen_at"]))
        conn.commit()
    print(f"backup {BACKUP}; deleted {deleted}, inserted {len(rows)}")


if __name__ == "__main__":
    main("--commit" in sys.argv)
