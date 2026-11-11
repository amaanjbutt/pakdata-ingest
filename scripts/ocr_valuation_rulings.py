"""OCR FBR valuation rulings whose PDF has no text layer, for `fbr_valuation_rulings`.

Most rulings from 2016-2020 are bare scans. OCR needs `rapidocr_onnxruntime` and `pymupdf`, which
the api image does not ship, so this runs on GitHub Actions (`customs.yml`, from `backend/`):

    pip install rapidocr_onnxruntime pymupdf
    python -m scripts.ocr_valuation_rulings --db [--limit 120]   # rulings the job couldn't read
    python -m scripts.ocr_valuation_rulings --pdf-dir DIR OUT.json ID [ID …]   # workstation test, no DB

The job lists every ruling and reads each PDF's own text layer; a ruling with none is stored with
parse_status 'unreadable', one whose text layer (the scanner's own OCR, often garbled) yields no
values with 'no_values'. This script OCRs both (scans first, newest first, `--limit` a run — the
backfill spreads over a few runs), one `customs_valuation_tokens` row per ruling, committed as it
goes; the job's next run parses the tokens. Pages are rendered at 200 dpi and straightened exactly as
for the ISMO merit orders (`scripts.ocr_merit_orders.ocr_pdf`).
"""
from __future__ import annotations

import json
import os
import sys


def main_db(limit: int) -> None:
    from psycopg.types.json import Jsonb
    from rapidocr_onnxruntime import RapidOCR

    from app import db
    from ingestion import http_client
    from scripts.ocr_merit_orders import ocr_pdf

    todo = db.query(
        """SELECT r.ruling_id, r.pdf_url FROM customs_valuation_rulings r
           LEFT JOIN customs_valuation_tokens t USING (ruling_id)
           WHERE (r.parse_status = 'unreadable' OR (r.parse_status = 'no_values' AND r.text_source = 'text'))
             AND t.ruling_id IS NULL
           ORDER BY r.parse_status = 'unreadable' DESC, r.listed_on DESC LIMIT %s""", (limit,))
    eng, n = RapidOCR(), 0
    for r in todo:
        try:
            content = http_client.get(r["pdf_url"].replace(" ", "%20"), timeout=120).content
        except Exception as exc:  # noqa: BLE001 — one dead link mustn't stop the batch
            print(f"{r['ruling_id']}: {exc}", flush=True)
            continue
        if content[:4] != b"%PDF":
            continue
        db.execute("INSERT INTO customs_valuation_tokens (ruling_id, pdf_url, pages) VALUES (%s, %s, %s) "
                   "ON CONFLICT (ruling_id) DO NOTHING", (r["ruling_id"], r["pdf_url"], Jsonb(ocr_pdf(content, eng))))
        n += 1
        print(r["ruling_id"], flush=True)
    left = db.query_one(
        """SELECT count(*) AS n FROM customs_valuation_rulings r LEFT JOIN customs_valuation_tokens t USING (ruling_id)
           WHERE (r.parse_status = 'unreadable' OR (r.parse_status = 'no_values' AND r.text_source = 'text'))
             AND t.ruling_id IS NULL""")["n"]
    print(f"{n} rulings OCR'd; {left} still to OCR")


def main_files(pdf_dir: str, out: str, ids: list[str]) -> None:
    from rapidocr_onnxruntime import RapidOCR

    from scripts.ocr_merit_orders import ocr_pdf

    bundle = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    eng = RapidOCR()
    for rid in ids:
        if rid in bundle:
            continue
        bundle[rid] = ocr_pdf(open(os.path.join(pdf_dir, f"{rid}.pdf"), "rb").read(), eng)
        json.dump(bundle, open(out, "w", encoding="utf-8"))  # checkpoint after each ruling
        print(rid, flush=True)


if __name__ == "__main__":
    if "--db" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 120
        main_db(lim)
    elif "--pdf-dir" in sys.argv:
        i = sys.argv.index("--pdf-dir")
        main_files(sys.argv[i + 1], sys.argv[i + 2], sys.argv[i + 3:])
    else:
        print(__doc__)
        sys.exit(1)
