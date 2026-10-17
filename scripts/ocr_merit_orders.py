"""OCR ISMO's scanned Economic Merit Order PDFs into a token bundle for `ismo_merit_order`.

The merit orders are photographed tables (one image per page, no text layer). This script runs
on a workstation in a venv that has `rapidocr_onnxruntime` and `pymupdf` (the api image does not
ship an OCR engine):

    python -m venv ocrenv && ocrenv/Scripts/pip install rapidocr_onnxruntime pymupdf curl_cffi pillow
    ocrenv/Scripts/python scripts/ocr_merit_orders.py ismo_merit_tokens.json [--only-new]

It lists every merit-order document through ISMO's documents API (public bearer token read from
the site's own bundle, as the dashboard does), renders each table page at 200 dpi, straightens it
(the scans are tilted ~0.5°: the angle that maximises the variance of the row-sum profile), runs
OCR and stores every token's box, text and confidence. Pages are cached by URL, so re-runs only
OCR new documents. Turning tokens into rows — and checking that fuel + VO&M + other = specific
cost on every row — is the job's pure `parse`, which is unit-tested.
"""
from __future__ import annotations

import json
import os
import re
import sys

SITE = "https://www.ismo.gov.pk"


def _session():
    from curl_cffi import requests as cr

    return cr.Session(impersonate="chrome")


def list_documents(s) -> list[dict]:
    home = s.get(SITE + "/", timeout=60).text
    js = re.search(r'src="(/static/js/main\.[0-9a-f]+\.js)"', home).group(1)
    tok = re.search(r'Authorization:"Bearer ([A-Za-z0-9]+)"', s.get(SITE + js, timeout=90).text).group(1)
    docs = s.get(f"{SITE}/api/v1/documents", headers={"Authorization": "Bearer " + tok}, timeout=90).json()["data"]
    return [d for d in docs if d.get("category") == "merit-order" and d.get("files")]


def deskew(img):
    """Rotate a PIL greyscale page so table rows are horizontal. Pure-ish (PIL + numpy)."""
    import numpy as np
    from PIL import Image

    small = img.resize((img.width // 4, img.height // 4))
    best, best_a = -1.0, 0.0
    for a10 in range(-30, 31):
        a = a10 / 10
        arr = 255 - np.asarray(small.rotate(a, resample=Image.BILINEAR, fillcolor=255), dtype=np.float32)
        score = float(arr.sum(axis=1).var())
        if score > best:
            best, best_a = score, a
    return img.rotate(best_a, resample=Image.BICUBIC, fillcolor=255, expand=False), best_a


def ocr_pdf(content: bytes, eng) -> list[dict]:
    import numpy as np
    import pymupdf
    from PIL import Image

    pages = []
    doc = pymupdf.open(stream=content, filetype="pdf")
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
        img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        img, angle = deskew(img)
        res, _ = eng(np.asarray(img.convert("RGB")))
        toks = [{"box": [[round(float(x), 1), round(float(y), 1)] for x, y in b], "text": t, "score": round(float(sc), 3)}
                for b, t, sc in (res or [])]
        pages.append({"page": i, "angle": angle, "width": pix.width, "height": pix.height, "tokens": toks})
    return pages


def main(out_path: str, only_new: bool = False) -> None:
    from rapidocr_onnxruntime import RapidOCR

    bundle = json.load(open(out_path, encoding="utf-8")) if os.path.exists(out_path) else {"documents": []}
    done = {d["url"] for d in bundle["documents"]}
    s, eng = _session(), RapidOCR()
    for d in sorted(list_documents(s), key=lambda d: d.get("date") or ""):
        url = d["files"][0]
        if url in done:
            continue
        content = s.get(url.replace(" ", "%20"), timeout=300).content
        print(d.get("date", "")[:10], d["title"], flush=True)
        bundle["documents"].append({"title": d["title"], "date": d.get("date"), "url": url,
                                    "pages": ocr_pdf(content, eng)})
        json.dump(bundle, open(out_path, "w", encoding="utf-8"))  # checkpoint after each document
    print(len(bundle["documents"]), "documents in", out_path)


if __name__ == "__main__":
    main(sys.argv[1], "--only-new" in sys.argv)
