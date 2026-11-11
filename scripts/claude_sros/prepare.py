"""Prepare and merge Claude's transcription of FBR customs SROs (regulatory duty tables and amendments)
read from page images — the SROs whose PDFs are bare scans, or whose scanner OCR drops decimal points.

Run on a workstation with pymupdf (`pip install pymupdf`):

    python -m scripts.claude_sros.prepare pages WORKDIR NAME URL   # download the SRO, render every page
                                                                   # (110 dpi greyscale) to WORKDIR/img/NAME_pNN.png
    # then have Claude follow INSTRUCTIONS.md (Claude Code subagents, ~8 pages each) and write
    # WORKDIR/out/NAME_pAA-BB.json (a table's parts) or WORKDIR/out/NAME.json (a short amendment)
    python -m scripts.claude_sros.prepare merge WORKDIR            # fold WORKDIR/out/*.json into
                                                                   # data/customs/regulatory_duty_claude.json

`merge` joins each SRO's parts in page order, folds a row continued from the previous page into that row,
and keeps the SROs already in the file (a re-transcribed SRO replaces its entry). `fbr_regulatory_duty`
re-checks every row against the year's tariff before storing it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data", "customs", "regulatory_duty_claude.json")
SOURCE = ("Transcribed by Claude from FBR's SRO page images (download1.fbr.gov.pk/SROs), 2026-11; the tables of SROs "
          "with a scanner text layer were checked page by page against its digits; fbr_regulatory_duty checks every "
          "code against that year's tariff and every amendment's 'for the figure …' against the table it amends.")


def pages(work: str, name: str, url: str) -> None:
    import pymupdf

    from ingestion import http_client

    os.makedirs(f"{work}/img", exist_ok=True)
    doc = pymupdf.open(stream=http_client.get(url, timeout=120).content, filetype="pdf")
    for i in range(doc.page_count):
        doc[i].get_pixmap(dpi=110, colorspace=pymupdf.csGRAY).save(f"{work}/img/{name}_p{i + 1:02d}.png")
    print(f"{doc.page_count} pages under {work}/img")


def fold(rows: list[dict]) -> list[dict]:
    """A row continued from the previous page (no S. No., `continued`) joins the row before it. Pure."""
    out: list[dict] = []
    for r in rows:
        if r.get("continued") and not r.get("sno") and not r.get("codes") and out:
            out[-1]["description"] = f"{out[-1].get('description', '')} {r.get('description', '')}".strip()
            if not out[-1].get("rate") and r.get("rate"):
                out[-1]["rate"] = r["rate"]
            continue
        out.append({k: v for k, v in r.items() if k not in ("page", "continued")})
    return out


def merge(work: str) -> None:
    data = {"source": SOURCE, "sros": {}}
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            data = json.load(f)
    parts: dict[str, list[dict]] = {}
    for path in glob.glob(f"{work}/out/*.json"):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        parts.setdefault(re.sub(r"\s+", "", d["sro"]), []).append(d)
    for sro, ds in parts.items():
        ds.sort(key=lambda d: min(d.get("pages") or [0]))
        rows = [r for d in ds for r in sorted(d["rows"], key=lambda r: r.get("page", 0))]
        entry = {k: ds[0].get(k) for k in ("dated", "amends", "effective") if ds[0].get(k)}
        entry["pages"] = max(p for d in ds for p in (d.get("pages") or [1]))
        entry["rows"] = fold(rows)
        entry["text"] = [t["text"] for d in ds for t in d.get("text", [])]
        notes = [d["notes"] for d in ds if d.get("notes")]
        if notes:
            entry["notes"] = " | ".join(notes)
        data["sros"][sro] = entry
    data["source"] = SOURCE
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{len(parts)} SROs merged; {len(data['sros'])} in {DATA}")


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "pages":
        pages(sys.argv[2], sys.argv[3], sys.argv[4])
    elif len(sys.argv) == 3 and sys.argv[1] == "merge":
        merge(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
