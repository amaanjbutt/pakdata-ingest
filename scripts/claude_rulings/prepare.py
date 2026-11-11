"""Prepare and merge Claude's transcription of FBR valuation rulings (see fbr_valuation_rulings' docstring).

Run on a workstation with pymupdf (`pip install pymupdf`):

    python -m scripts.claude_rulings.prepare pages WORKDIR [IDS…]   # download the PDFs, render every page
                                                                    # (≤ 8, 100 dpi greyscale) and write
                                                                    # WORKDIR/in/batch_NNN.json (~30 pages each)
    # then, per batch, have Claude follow INSTRUCTIONS.md (Claude Code subagents, one batch each) and write
    # WORKDIR/out/batch_NNN.json
    python -m scripts.claude_rulings.prepare merge WORKDIR [OUT IN] # fold WORKDIR/out/*.json into
                                                                    # data/customs/valuation_rulings_claude.json

Without IDS, `pages` takes every ruling the rulings job couldn't read fully (parse_status no_values /
partial / unreadable) and that the data file doesn't cover yet, from the database. The job re-checks
every transcribed row before storing it.
"""
from __future__ import annotations

import json
import os
import sys

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data", "customs", "valuation_rulings_claude.json")


def pages(work: str, ids: list[int]) -> None:
    import pymupdf

    from ingestion import http_client
    from ingestion.jobs.fbr_valuation_rulings import load_claude, pdf_candidates

    if not ids:
        from app import db

        have = set(load_claude())
        ids = [r["ruling_id"] for r in db.query(
            "SELECT ruling_id FROM customs_valuation_rulings WHERE parse_status IN ('no_values', 'partial', 'unreadable') "
            "AND kind NOT IN ('withdrawal', 'public_notice', 'classification') ORDER BY ruling_id")
            if r["ruling_id"] not in have]
    from app import db

    meta = {r["ruling_id"]: r for r in db.query(
        "SELECT ruling_id, number_raw, title, pdf_url FROM customs_valuation_rulings WHERE ruling_id = ANY(%s)", (ids,))}
    os.makedirs(f"{work}/img", exist_ok=True)
    os.makedirs(f"{work}/in", exist_ok=True)
    os.makedirs(f"{work}/out", exist_ok=True)
    items, batch, n_pages, n = [], [], 0, 0
    for rid in ids:
        m = meta.get(rid)
        if not m:
            continue
        content = None
        for u in pdf_candidates(m["pdf_url"], m["number_raw"]):
            try:
                b = http_client.get(u, timeout=120).content
            except Exception:  # noqa: BLE001
                continue
            if b[:4] == b"%PDF":
                content = b
                break
        if content is None:
            print(rid, "no PDF")
            continue
        doc = pymupdf.open(stream=content, filetype="pdf")
        files = []
        for i in range(min(doc.page_count, 8)):
            f = os.path.abspath(f"{work}/img/{rid}_p{i + 1}.png")
            doc[i].get_pixmap(dpi=100, colorspace=pymupdf.csGRAY).save(f)
            files.append(f)
        if batch and n_pages + len(files) > 30:
            items.append(batch)
            batch, n_pages = [], 0
        batch.append({"ruling_id": rid, "number": m["number_raw"], "title": m["title"], "pages": files})
        n_pages += len(files)
        n += 1
    if batch:
        items.append(batch)
    for i, b in enumerate(items):
        with open(f"{work}/in/batch_{i:03d}.json", "w", encoding="utf-8") as f:
            json.dump(b, f, indent=1)
    print(f"{n} rulings in {len(items)} batches under {work}/in")


def merge(work: str, out_dir: str = "out", in_dir: str = "in") -> None:
    """Fold WORK/<out_dir>/*.json into the data file. Rulings already in the file keep their entry.
    A letter that prints another ruling's number (FBR's list links the next ruling's PDF in places)
    is filed under the listing with that number when exactly one batch input carries it; the rest of
    the mismatches are reported, and the job stores none of their rows."""
    import glob

    from ingestion.jobs.fbr_valuation_rulings import norm_number

    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    listed: dict[int, str | None] = {}
    for path in glob.glob(f"{work}/{in_dir}/*.json"):
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                listed[r["ruling_id"]] = norm_number(r.get("number"))
    by_number: dict[str, list[int]] = {}
    for rid, num in listed.items():
        if num:
            by_number.setdefault(num, []).append(rid)
    found: dict[int, dict] = {}
    moved, unresolved = [], []
    for path in sorted(glob.glob(f"{work}/{out_dir}/*.json")):
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                rid, target = r["ruling_id"], r["ruling_id"]
                printed, mine = norm_number(r.get("number_printed")), listed.get(r["ruling_id"])
                own = not (printed and mine and printed != mine)
                if not own:
                    cands = by_number.get(printed, [])
                    if len(cands) == 1:
                        target = cands[0]
                        moved.append((rid, mine, target, printed))
                    else:
                        unresolved.append((rid, mine, printed, len(cands)))
                if target != rid and target in found and found[target].get("_own"):
                    continue
                if target == rid and target in found and found[target].get("_own") and not own:
                    continue
                found[target] = dict(r, _own=own or target != rid)
    for rid, mine, _, printed in moved:
        # the listing whose link opens another ruling's letter: an empty entry, so the job doesn't
        # read that letter as this ruling's
        found.setdefault(rid, {"number_printed": printed, "supersedes": [], "rows": [], "_own": True,
                               "no_values_reason": f"FBR's link for {mine} opens ruling {printed}'s letter"})
    added = 0
    for rid, r in found.items():
        if str(rid) in data["rulings"]:
            continue
        data["rulings"][str(rid)] = {k: r.get(k) for k in (
            "dated", "number_printed", "supersedes", "unit_header", "rows", "no_values_reason", "notes",
            "listed_number_typo") if r.get(k) is not None or k != "listed_number_typo"}
        added += 1
    data["rulings"] = dict(sorted(data["rulings"].items(), key=lambda kv: int(kv[0])))
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{added} rulings added; {len(data['rulings'])} in {DATA}")
    print(f"{len(moved)} letters filed under the listing with their printed number:", moved)
    print(f"{len(unresolved)} letters whose printed number matches no single listing (not stored by the job):",
          unresolved)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "pages":
        pages(sys.argv[2], [int(a) for a in sys.argv[3:]])
    elif len(sys.argv) in (3, 5) and sys.argv[1] == "merge":
        merge(sys.argv[2], *sys.argv[3:5])
    else:
        print(__doc__)
        sys.exit(1)
