"""dq_fetch_parse — the workstation half of the data-quality audit (scripts/dq_audit.py).

Fetches a publisher's files from a machine that can reach the source (PBS, PTA and MUFAP
block the VPS) and parses them with the ingestion job's own parser into a records file.
Nothing touches the database here; the file goes to the VPS and
`python -m scripts.dq_audit compare-records <method> records.json` compares it there.

  python scripts/dq_fetch_parse.py spi out.json [--cache DIR]   # every SPI annexure PBS lists
  python scripts/dq_fetch_parse.py job <job_name> out.json [--backfill]
                                                     # a job's own fetch() + parse()
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingestion.framework import canonicalize_id, dedupe_last  # noqa: E402


def _dump(records, out: str) -> None:
    rows = [{"series_id": canonicalize_id(r.series_id), "obs_date": r.obs_date.isoformat(),
             "value": r.value, "dims": r.dims or {}} for r in dedupe_last(records)]
    Path(out).write_text(json.dumps(rows), encoding="utf-8")
    dates = sorted({r["obs_date"] for r in rows})
    print(f"{len(rows)} records, {len({r['series_id'] for r in rows})} series, "
          f"{dates[0] if dates else '-'}..{dates[-1] if dates else '-'} -> {out}")


def spi(out: str, cache: str | None) -> None:
    from ingestion import http_client
    from ingestion.jobs.pbs_spi_weekly import ARCHIVE_PAGE, archive_entries, parse_spi_pdf

    page = http_client.get(ARCHIVE_PAGE, timeout=90).text
    entries = archive_entries(page)
    print(f"{len(entries)} weeks listed by PBS ({entries[0][0]}..{entries[-1][0]})")
    records, missing = [], []
    for d, url in entries:
        cached = Path(cache) / f"{d.isoformat()}.pdf" if cache else None
        content = cached.read_bytes() if cached and cached.exists() else None
        if content is None:
            try:
                resp = http_client.get(url, timeout=90)
                content = resp.content if resp.content[:4] == b"%PDF" else None
            except Exception:  # noqa: BLE001
                content = None
            if content and cached:
                cached.write_bytes(content)
        if content is None:
            missing.append(d.isoformat())
            continue
        try:
            _, recs = parse_spi_pdf(content, d)
            records += recs
        except Exception as exc:  # noqa: BLE001
            missing.append(f"{d} (parse: {exc})")
    print(f"unavailable at PBS: {len(missing)} {missing[:15]}")
    _dump(records, out)


def job(name: str, out: str, backfill: bool) -> None:
    from ingestion.registry import JOBS

    j = JOBS[name]()
    files = j.fetch(backfill=backfill)
    records = []
    for f in files:
        try:
            records += j.parse(f)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {f.filename}: {exc}")
    print(f"{len(files)} files from {name}")
    _dump(records, out)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True) for a in sys.argv[1:] if a.startswith("--")}
    if args[:1] == ["spi"] and len(args) == 2:
        spi(args[1], flags.get("--cache") if isinstance(flags.get("--cache"), str) else None)
    elif args[:1] == ["job"] and len(args) == 3:
        job(args[1], args[2], bool(flags.get("--backfill")))
    else:
        print(__doc__)
        sys.exit(1)
