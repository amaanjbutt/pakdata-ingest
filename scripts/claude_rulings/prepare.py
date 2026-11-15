"""Prepare and merge Claude's transcription of FBR valuation rulings (see fbr_valuation_rulings' docstring).

Run on a workstation with pymupdf (`pip install pymupdf`):

    python -m scripts.claude_rulings.prepare pages WORKDIR [IDS…]   # download the PDFs, render every page
                                                                    # (≤ 8, 100 dpi greyscale) and write
                                                                    # WORKDIR/in/batch_NNN.json (~30 pages each)
    # then, per batch, have Claude follow INSTRUCTIONS.md (Claude Code subagents, one batch each) and write
    # WORKDIR/out/batch_NNN.json
    python -m scripts.claude_rulings.prepare merge WORKDIR [OUT IN] [--replace=ID,ID]
                                                                    # fold WORKDIR/out/*.json into
                                                                    # data/customs/valuation_rulings_claude.json

The 2026-11 second half (rulings 470 → 984) was transcribed by DeepSeek from 150 dpi page images (a kit in
the owner's PC: these instructions, a validator and a cross-check of every value and code against an OCR
text of its page), checked by Claude on a sample, and merged here.

Without IDS, `pages` takes every ruling the rulings job couldn't read fully (parse_status no_values /
partial / unreadable) and that the data file doesn't cover yet, from the database. The job re-checks
every transcribed row before storing it.
"""
from __future__ import annotations

import json
import os
import sys

DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data", "customs", "valuation_rulings_claude.json")


def dump_data(data: dict, path: str = DATA) -> None:
    """The data file with one compact line per ruling: small, and a diff shows which rulings changed."""
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"source": ' + json.dumps(data["source"], ensure_ascii=False) + ',\n "rulings": {\n')
        items = sorted(data["rulings"].items(), key=lambda kv: int(kv[0]))
        f.write(",\n".join(f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}"
                           for k, v in items))
        f.write("\n }\n}\n")


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


_STOP = {"determination", "customs", "custom", "value", "values", "valuation", "ruling", "rulings", "under",
         "section", "1969", "origin", "origins", "with", "types", "kinds", "including", "other", "others",
         "imported", "import", "goods", "items", "item", "type", "kind", "regarding", "read", "from", "into",
         "their", "publication", "addendum", "corrigendum", "correction", "revision", "order", "advice", "made"}


def _words(text: str) -> set[str]:
    """Content words of a title or of a letter's goods, for telling two subjects apart. Pure."""
    import re

    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in _STOP}


def same_subject(title: str | None, rows: list[dict]) -> bool:
    """Does a listing's title name the goods the letter values? Pure."""
    goods = _words(" ".join(f"{r.get('item', '')} {r.get('variant') or ''}" for r in rows))
    return bool(_words(title) & goods)


def merge(work: str, out_dir: str = "out", in_dir: str = "in", replace: set[int] | None = None) -> None:
    """Fold WORK/<out_dir>/*.json into the data file. Rulings already in the file keep their entry, unless
    their id is in `replace` (a re-transcription). Batch inputs carry the listing's number as
    `expected_number` (or `number`) and its `title`.

    A letter that prints another ruling's number:
      - is filed under the listing that carries the printed number when exactly one batch input does AND
        that listing's title names the letter's goods (in a stretch of FBR's 2017 list each entry links the
        next ruling's PDF); that listing's own entry gets no values,
      - else stays under its own listing as `listed_number_typo` when its own title names the goods (FBR's
        list or the letter mistyped the number: 1508/2021 printed "1508/2020", 5/2022 printed "05/2023"),
      - else is reported and the job stores none of its rows.
    A letter without values is never re-filed."""
    import glob

    from ingestion.jobs.fbr_valuation_rulings import norm_number

    replace = replace or set()
    with open(DATA, encoding="utf-8") as f:
        data = json.load(f)
    listed: dict[int, str | None] = {}
    titles: dict[int, str] = {}
    for path in glob.glob(f"{work}/{in_dir}/*.json"):
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                listed[r["ruling_id"]] = norm_number(r.get("expected_number") or r.get("number"))
                titles[r["ruling_id"]] = r.get("title") or ""
    by_number: dict[str, list[int]] = {}
    for rid, num in listed.items():
        if num:
            by_number.setdefault(num, []).append(rid)
    found: dict[int, dict] = {}
    moved, typos, unresolved = [], [], []
    for path in sorted(glob.glob(f"{work}/{out_dir}/*.json")):
        with open(path, encoding="utf-8") as f:
            for r in json.load(f):
                rid = target = r["ruling_id"]
                printed, mine = norm_number(r.get("number_printed")), listed.get(rid)
                rows = r.get("rows") or []
                entry = dict(r)
                if printed and mine and printed != mine and rows:
                    cands = [c for c in by_number.get(printed, []) if c != rid]
                    if len(cands) == 1 and same_subject(titles.get(cands[0]), rows):
                        target = cands[0]
                        moved.append((rid, mine, target, printed))
                    elif same_subject(titles.get(rid), rows):
                        entry["listed_number_typo"] = True
                        typos.append((rid, mine, printed))
                    else:
                        unresolved.append((rid, mine, printed))
                own = target == rid
                if target in found and found[target]["_own"] and not own:
                    continue          # the listing's own letter wins over a re-filed one
                if target in found and not found[target]["_own"] and own:
                    pass              # its own letter replaces a re-filed one
                found[target] = dict(entry, _own=own)
    for rid, mine, _, printed in moved:
        # the listing whose link opens another ruling's letter: an empty entry, so the job doesn't
        # read that letter as this ruling's
        found.setdefault(rid, {"number_printed": printed, "supersedes": [], "rows": [], "_own": True,
                               "no_values_reason": f"FBR's link for {mine} opens ruling {printed}'s letter"})
    added = 0
    keys = ("dated", "number_printed", "supersedes", "unit_header", "rows", "no_values_reason", "notes",
            "listed_number_typo")
    for rid, r in found.items():
        if str(rid) in data["rulings"] and rid not in replace:
            continue
        data["rulings"][str(rid)] = {k: r.get(k) for k in keys if r.get(k) is not None or k != "listed_number_typo"}
        added += 1
    dump_data(data)
    print(f"{added} rulings added or replaced; {len(data['rulings'])} in {DATA}")
    print(f"{len(moved)} letters filed under the listing with their printed number:", moved)
    print(f"{len(typos)} letters kept under their own listing (number mistyped, same goods):", typos)
    print(f"{len(unresolved)} letters whose printed number matches no listing with the same goods "
          "(not stored by the job):", unresolved)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "pages":
        pages(sys.argv[2], [int(a) for a in sys.argv[3:]])
    elif len(sys.argv) >= 3 and sys.argv[1] == "merge":
        args = [a for a in sys.argv[3:] if not a.startswith("--replace=")]
        rep = {int(x) for a in sys.argv[3:] if a.startswith("--replace=") for x in a[10:].split(",") if x}
        merge(sys.argv[2], *args[:2], replace=rep)
    else:
        print(__doc__)
        sys.exit(1)
