"""Revision vintages for WAPDA's river flows, from every copy of its river-flow tables.

WAPDA overwrites its river-flow table in place, so the only record of what it printed earlier
is the Internet Archive's captures of its old web pages (Apr 2017 – Feb 2023) and the workbook
(uploaded 20 Dec 2024). Each copy is an "issue" published on its capture date (a workbook copy
other than the 2024 upload: the day it was fetched); a day's figure in successive copies is its
chain of prints (first print, then any revision). This builds the vintage source JSON with pinned
rows (series + dims named outright, see scripts/vintages_core.py) — the same parsers as
`ingestion/jobs/wapda_river_flows.py`, so the newest print is exactly the value the job stored.

Run where wapda.gov.pk answers (the owner's PC), after the job's backfill is loaded:

  python -m scripts.vintages_wapda build OUT.json [PREVIOUS.json]
  # on the VPS: store it with the other sources, then rebuild (a FULL rebuild of every source)
  python -m ingestion.jobs.sbp_msb_vintages load /tmp/wapda_web.json
  python -m ingestion.run vintages_refresh --backfill

Pass the previous file (`SELECT payload FROM vintage_source_files WHERE name = 'wapda_web.json'`)
when WAPDA has changed the workbook: its copies are kept and the new one is added on top — the
archive of every copy we have seen. Only a print that is new or differs from the point's previous
print is written (the chain is unchanged, the file ~2 MB instead of ~10).
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.jobs.wapda_river_flows import Print, file_prints, workbook_sha  # noqa: E402
from scripts.vintages_core import same_print  # noqa: E402

SOURCE = "wapda_web"


def _same(a: tuple[float, int], b: tuple[float, int]) -> bool:
    return same_print((None, a[0], a[1], 1.0), (None, b[0], b[1], 1.0))


def vintage_issues(copies: list[tuple[str, date, list[Print]]], previous: list[dict] | None = None) -> list[dict]:
    """[(issue id, published, prints)] -> vintages_core issues with pinned rows, oldest first, each
    print kept only if new or changed since the point's last print. `previous` issues (an earlier
    build) are kept as they are; a copy whose issue id is already among them is skipped. Pure."""
    issues = [dict(i) for i in previous or []]
    have = {i["issue"] for i in issues}
    last: dict[tuple, tuple[float, int]] = {}
    for iss in sorted(issues, key=lambda i: (i["published"], i["issue"])):
        for r in iss["rows"]:
            dims = tuple(sorted(r["dims"].items()))
            for d, (v, dp) in r["values"].items():
                last[(r["series_id"], dims, date.fromisoformat(d))] = (v, dp)
    for issue, published, prints in sorted(copies, key=lambda c: (c[1], c[0])):
        if issue in have:
            continue
        rows: dict[tuple, dict] = {}
        for p in prints:
            point = (p.series_id, p.dims, p.obs_date)
            if point in last and _same(last[point], (p.value, p.dp)):
                continue
            last[point] = (p.value, p.dp)
            key = (p.series_id, p.dims, p.scale)
            row = rows.setdefault(key, {
                "label": f"{p.series_id} " + " ".join(f"{k}={v}" for k, v in p.dims),
                "series_id": p.series_id, "dims": dict(p.dims), "scale": p.scale, "values": {}})
            row["values"][p.obs_date.isoformat()] = [p.value, p.dp]
        if rows:
            issues.append({"issue": issue, "published": published.isoformat(), "source": SOURCE,
                           "freq": "daily", "rows": list(rows.values())})
    return sorted(issues, key=lambda i: (i["published"], i["issue"]))


def build(out: str, previous: str | None = None) -> None:
    from ingestion.jobs.wapda_river_flows import WapdaRiverFlowsJob

    copies = []
    for f in WapdaRiverFlowsJob().fetch(backfill=True):
        try:
            published, prints = file_prints(f)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {f.filename}: {exc}")
            continue
        issue = (f.filename.rsplit(".", 1)[0] if f.filename.startswith("wayback_")
                 else f"workbook-{workbook_sha(f.content)}")
        copies.append((issue, published, prints))
    prev = json.loads(Path(previous).read_text(encoding="utf-8")) if previous else None
    issues = vintage_issues(copies, prev)
    Path(out).write_text(json.dumps(issues), encoding="utf-8")
    n = sum(len(r["values"]) for i in issues for r in i["rows"])
    print(f"{len(copies)} copies fetched -> {len(issues)} issues, {n} prints -> {out}")


if __name__ == "__main__":
    if len(sys.argv) in (3, 4) and sys.argv[1] == "build":
        build(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    else:
        print(__doc__)
        sys.exit(1)
