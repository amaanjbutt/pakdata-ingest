"""Revision vintages for Port Qasim's annual port performance table, from every copy of it.

PQA rewrites its "Port Performance Data of Last 5 years" table in place (the newest fiscal year is provisional
and gets restated), so the only record of what it printed earlier is the Internet Archive's captures of the
page (43 distinct copies, June 2019 →). Each copy is an "issue" published on its capture date; a fiscal
year's figure in successive copies is its chain of prints (first print, then any restatement). This builds
the vintage source JSON with pinned rows (series + dims named outright, see scripts/vintages_core.py) with
the job's own parser, so the newest print is exactly the value `pqa_port_performance --backfill` stored.

  python -m scripts.vintages_pqa build OUT.json
  # on the VPS: store it with the other sources, then rebuild (a FULL rebuild of every source)
  python -m ingestion.jobs.sbp_msb_vintages load /tmp/pqa_performance.json
  python -m ingestion.run vintages_refresh --backfill
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.jobs.pqa_port_performance import PqaPortPerformanceJob, parse  # noqa: E402

SOURCE = "pqa_performance"


def issues_from(copies: list[tuple[str, date, str]]) -> list[dict]:
    """[(issue id, published, page html)] -> vintages_core issues, oldest first; a print is kept only when
    it is new or differs from the point's previous print. Pure."""
    last: dict[tuple, float] = {}
    out = []
    for issue, published, page in sorted(copies, key=lambda c: (c[1], c[0])):
        try:
            _, recs = parse(page)
        except ValueError:
            continue
        rows: dict[tuple, dict] = {}
        for r in recs:
            dims = tuple(sorted(r.dims.items()))
            point = (r.series_id, dims, r.obs_date)
            if last.get(point) == r.value:
                continue
            last[point] = r.value
            scale = 1.0 if r.series_id == "ports.qasim.ships_annual" else 1000.0
            row = rows.setdefault((r.series_id, dims), {
                "label": f"{r.series_id} " + " ".join(f"{k}={v}" for k, v in dims),
                "series_id": r.series_id, "dims": dict(r.dims), "scale": scale, "values": {}})
            row["values"][r.obs_date.isoformat()] = [round(r.value / scale, 3), 0]
        if rows:
            out.append({"issue": issue, "published": published.isoformat(), "source": SOURCE, "freq": "annual",
                        "rows": list(rows.values())})
    return out


def build(out: str) -> None:
    copies = []
    for f in PqaPortPerformanceJob().fetch(backfill=True):
        if f.filename.startswith("wayback_"):
            ts = f.filename[len("wayback_"):].split(".")[0]
            published = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        else:
            published = f.when
        copies.append((f.filename.rsplit(".", 1)[0], published, f.content.decode("utf-8", "replace")))
    issues = issues_from(copies)
    Path(out).write_text(json.dumps(issues), encoding="utf-8")
    n = sum(len(r["values"]) for i in issues for r in i["rows"])
    print(f"{len(copies)} copies -> {len(issues)} issues, {n} prints -> {out}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "build":
        build(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
