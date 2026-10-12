"""Backfill revision vintages from SBP's Monthly Statistical Bulletin archive.

Every monthly issue of the Bulletin opens with "Selected Economic Indicators": ~30
headline series (M2, exports/imports, FDI, reserves, remittances, REER/NEER, the
exchange rate, CPI inflation, core inflation, tax collection, ...) for the last ~13
months, *as published that month*. With issues back to April 2005, each month's figure
appears in a dozen consecutive issues — its first print and every revision, each with a
date. That is the point-in-time history SBP's live data no longer shows (a revision
overwrites the original), and it is what makes ?vintage= answer "what did the official
number say on date X" for years before we started tracking revisions ourselves
(2026-08-17).

Three steps (run from the repo root):

  1. download  — crawl SBP's bulletin index (one page per issue, Apr 2005 ->) and fetch
                 each issue's "Selected Economic Indicators" PDF (run where SBP is
                 reachable; ~1.5 s between requests). Also writes MANIFEST.json: every
                 chapter file of every issue, for the chapter-table adapters:
                   python -m scripts.vintages_sbp_msb download DIR
  2. parse     — PDFs -> one JSON of {issue, published, source, freq, rows:[{label, values}]}:
                   python -m scripts.vintages_sbp_msb parse DIR OUT.json
  3. apply     — needs the database; see scripts/vintages_core.py (shared by every source):
                   python -m scripts.vintages_core apply OUT.json [OTHER.json ...] [--commit]
                   python -m scripts.vintages_core candidates OUT.json [...] CANDIDATES.csv

Rollback: see scripts/vintages_core.py.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Re-exported: the matching/chain logic lives in the shared core (tests import it here).
from scripts.vintages_core import (  # noqa: F401
    _MON_IDX, MONTHS, SCALES, _same, _sig3, apply, build_index, build_vintages,
    build_vintages_detailed, drop_blips, drop_unconfirmed_outliers, label_family, map_row,
    month_end, near_match,
)

ARCHIVE = "https://archive.sbp.org.pk/reports/stat_reviews/Bulletin/{y}/{m}/{f}"
INDEX = "https://www.sbp.org.pk/our-operations/publications/statistical-bulletin/412"
FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]
# The indicators chapter's file name changed over the years.
IND_NAMES = ("IND.pdf", "Indicator.pdf", "Selected_Economic-Indicators.pdf",
             "Selected_Economic_Indicators.pdf", "Indicators.pdf")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
SOURCE = "sbp_msb"
_ISSUE_SLUG = re.compile(r"/statistical-bulletin/412/[a-z-]*?(" + "|".join(m.lower() for m in FULL_MONTHS)
                         + r")-(\d{4})(?=[\"'/?#])")
_LINK = re.compile(r"href=[\"']([^\"']+\.(?:pdf|xlsx?|zip))[\"']", re.I)


# ---- 1. download -------------------------------------------------------------------

def _get(url: str) -> tuple[int, bytes]:
    """SBP's new site is Cloudflare-fronted: plain httpx is challenged, a browser TLS
    fingerprint (curl_cffi, or curl with a browser UA) is not."""
    try:
        from curl_cffi import requests as cr

        r = cr.get(url, impersonate="chrome", timeout=120, headers={"User-Agent": UA})
        return r.status_code, r.content
    except ImportError:
        out = subprocess.run(["curl", "-s", "-L", "-A", UA, "--max-time", "120", "-w", "\n%{http_code}", url],
                             capture_output=True, check=False)
        body, _, code = out.stdout.rpartition(b"\n")
        return int(code or 0), body


def _abs(href: str) -> str:
    if href.startswith("http"):
        return re.sub(r"(?<!:)//+", "/", href)
    return "https://www.sbp.org.pk/" + href.lstrip("/")


def issue_pages(index_html: str) -> dict[str, str]:
    """Bulletin index -> {"YYYY-MM": issue page URL}."""
    out = {}
    for m in _ISSUE_SLUG.finditer(index_html):
        month, year = FULL_MONTHS.index(m.group(1).title()) + 1, int(m.group(2))
        start = index_html.rfind('"', 0, m.start()) + 1
        out[f"{year}-{month:02d}"] = index_html[start:m.end()]
    return out


def indicators_links(links: list[str]) -> list[str]:
    """Candidate files for the 'Selected Economic Indicators' table among an issue's
    chapter links, best first: its own chapter PDF, else chapter 1, else the full
    bulletin (the parser only reads the indicator pages of whatever it is given).
    Chapter numbers moved in 2026 (Aug: chapter 1 is debt), so the caller checks."""
    pdfs = [u for u in links if u.lower().endswith(".pdf")]
    out: list[str] = []
    for test in (lambda u: "selected_economic" in u.lower() or "selected-economic" in u.lower()
                 or re.search(r"[-_/]indicators?\.pdf$", u, re.I),
                 # chap-1-jan-2026.pdf, SB-26-Jul-Chap1.pdf, MSB_AUG_2026_chap1.pdf
                 lambda u: re.search(r"chap-?1(?:-[a-z]+-\d{4})?\.pdf$", u, re.I),
                 lambda u: "monthly_statistical_bulletin" in u.lower() or "complete" in u.lower()
                 or re.search(r"/MSB_[A-Za-z]+_\d{4}", u)):
        out += [u for u in pdfs if test(u) and u not in out]
    return out


def has_indicator_page(data: bytes) -> bool:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:  # noqa: BLE001 - not a readable PDF
        return False
    return any(_INDICATOR_PAGE.search(pg.get_textpage().get_text_range()) for pg in pdf)


def download(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    code, body = _get(INDEX)
    if code != 200:
        sys.exit(f"bulletin index {INDEX} -> HTTP {code}")
    pages = issue_pages(body.decode("utf-8", "replace"))
    print(f"{len(pages)} issues listed ({min(pages)} .. {max(pages)})")
    manifest_path = out / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    got = missing = 0
    for issue in sorted(pages):
        y, m = int(issue[:4]), int(issue[5:])
        f = out / f"IND_{y}_{MONTHS[m - 1]}.pdf"
        if issue not in manifest:
            time.sleep(1.5)
            code, html = _get(pages[issue])
            links = sorted({_abs(h) for h in _LINK.findall(html.decode("utf-8", "replace"))})
            # every issue page also carries the site-wide publications menu; keep only
            # files that belong to this issue (they name its month or sit in its era's paths)
            mon = FULL_MONTHS[m - 1].lower()
            own = [u for u in links
                   if (str(y) in u or f"-{y % 100:02d}" in u) and mon[:3] in u.lower()]
            manifest[issue] = {"page": pages[issue], "files": own}
            manifest_path.write_text(json.dumps(manifest, indent=1))
        if f.exists() and f.stat().st_size > 0:
            got += 1
            continue
        urls = indicators_links(manifest[issue]["files"]) + [
            ARCHIVE.format(y=y, m=MONTHS[m - 1], f=n) for n in IND_NAMES]
        for u in urls:
            time.sleep(1.5)
            code, data = _get(u)
            if code == 200 and data[:4] == b"%PDF" and has_indicator_page(data):
                f.write_bytes(data)
                got += 1
                break
        else:
            missing += 1
            print(f"  no indicators PDF for {issue}")
    print(f"{got} issues in {out}, {missing} missing")


# ---- 2. parse ------------------------------------------------------------------------

# A value token: "1,001.9", "(1,001.9)", "-7", "2,379." (stray trailing dot, 2008), or a
# mangled number like "73..3207" (kept so the row's columns stay aligned; it parses to
# None and is skipped).
_NUM_TOKEN = re.compile(r"^\(?-?[\d,.]*\d[\d,.]*\)?$")
_PLACEHOLDER = {"-", "--", "–", "—", "�", "…", "...", "..", "n.a.", "na", "n.a", "#"}
_ITEM_START = re.compile(r"^(\d{1,2})\.?\s+([A-Za-z].*)$")
_INDICATOR_PAGE = re.compile(r"^\s*1\.?\s+Currency in Circulation", re.M | re.I)
_FIRST_ROW = re.compile(r"\n\s*1\.?\s+Currency", re.I)


def _is_value(tok: str) -> bool:
    t = tok.strip().lower()
    return bool(_NUM_TOKEN.match(t)) or t in _PLACEHOLDER


def _to_number(tok: str) -> tuple[float | None, int]:
    """'(1,001.9)' -> (-1001.9, 1). Returns (value, decimals shown); placeholders -> (None, 0)."""
    t = tok.strip()
    if t.lower() in _PLACEHOLDER:
        return None, 0
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").replace(",", "").rstrip(".")
    if t.count(".") > 1:
        return None, 0
    try:
        v = float(t)
    except ValueError:
        return None, 0
    dp = len(t.split(".")[1]) if "." in t else 0
    return (-v if neg else v), dp


def _header_months(text: str, prev_last: tuple[int, int] | None = None,
                   issue: tuple[int, int] | None = None) -> tuple[list[tuple[int, int]], bool]:
    """Column periods on a page, oldest first, and whether a 'corresponding month a
    year ago' column precedes them. Year tokens in the header give the starting year;
    the year rolls over when the month sequence wraps (Dec -> Jan)."""
    m = _FIRST_ROW.search(text)
    head = text[:m.start()] if m else text.split("\n1 ", 1)[0]  # everything before the first row
    head = re.sub(r"^\s*1\.\s+Selected Economic Indicators", "", head, flags=re.M | re.I)
    corresponding = bool(re.search(r"correspond|year\s*ago", head, re.I))
    years = [int(y.group(0)) for y in re.finditer(r"\b(?:19|20)\d{2}\b", head)]
    # month tokens like "Jan", "Jan.", "Sept"
    toks = re.findall(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?(?=\s|$)", head)
    months = [_MON_IDX[t[:3].lower()] for t in toks]
    if not months:
        return [], corresponding
    if not years:
        # A continuation page with no year in its header (2026 layout): carry on
        # from the previous page's last column, else anchor the last column to the
        # most recent such month on or before the issue month.
        out = []
        if prev_last is not None:
            y, last_m = prev_last
            for m in months:
                if m <= last_m:
                    y += 1
                out.append((y, m))
                last_m = m
            return out, corresponding
        if issue is None:
            return [], corresponding
        y = issue[0] - (1 if months[-1] > issue[1] else 0)
        rev = []
        nxt = None
        for m in reversed(months):
            if nxt is not None and m >= nxt:
                y -= 1
            rev.append((y, m))
            nxt = m
        return list(reversed(rev)), corresponding
    # Base-year tokens in the header (e.g. "(2015-16=100)") are not column years;
    # column years sit just above the months — use the first year that makes the
    # sequence end no later than the last year mentioned.
    y = years[0]
    out: list[tuple[int, int]] = []
    prev = None
    for m in months:
        if prev is not None and m <= prev:
            y += 1
        out.append((y, m))
        prev = m
    if out and out[-1][0] > max(years) and (issue is None or out[-1] > issue):
        # the first year token belonged to an earlier header element; shift back.
        # Not when the columns as read already end by the issue month: a header that
        # names only the FIRST column's year ("2024 / Dec Jan Feb ..", 2026 layout)
        # rolls into the next year legitimately.
        shift = out[-1][0] - max(years)
        out = [(yy - shift, mm) for yy, mm in out]
    return out, corresponding


def _rows(text: str) -> list[tuple[str, list[str]]]:
    """Numbered rows -> (label, trailing value tokens). Continuation lines (wrapped
    units, values on the next line) are folded into the current row."""
    rows: list[list] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _ITEM_START.match(line)
        if m:
            rows.append([m.group(2)])
        elif rows:
            rows[-1].append(line)
    out = []
    for parts in rows:
        toks = " ".join(parts).split()
        vals: list[str] = []
        while toks and _is_value(toks[-1]):
            vals.insert(0, toks.pop())
        label = " ".join(toks)
        out.append((label, vals))
    return out


def parse_issue(pdf_path: Path) -> dict | None:
    import pypdfium2 as pdfium

    m = re.match(r"IND_(\d{4})_(\w{3})\.pdf", pdf_path.name)
    if not m:
        return None
    iy, im = int(m.group(1)), _MON_IDX[m.group(2).lower()]
    pdf = pdfium.PdfDocument(str(pdf_path))
    series: dict[str, dict[str, tuple[float, int]]] = defaultdict(dict)
    labels: dict[str, str] = {}
    prev_last: tuple[int, int] | None = None
    for page in pdf:
        text = page.get_textpage().get_text_range().replace("\r", "")
        # Only the indicators table (a full bulletin PDF has ~190 other pages of tables);
        # its first row is "1 Currency in Circulation" in every era (the heading is an
        # image in the 2026 layout, so it can't be the test).
        if not _INDICATOR_PAGE.search(text):
            continue
        cols, corresponding = _header_months(text, prev_last, (iy, im))
        if not cols:
            continue
        prev_last = cols[-1]
        for label, vals in _rows(text):
            if label.lower().startswith("selected economic indicators"):
                continue
            n = len(cols) + (1 if corresponding else 0)
            if len(vals) < n:
                continue
            vals = vals[-n:]
            if corresponding:
                vals = vals[1:]
            for (cy, cm), tok in zip(cols, vals):
                v, dp = _to_number(tok)
                if v is None:
                    continue
                # A column can't be later than the issue month itself.
                if (cy, cm) > (iy, im):
                    continue
                key = re.sub(r"[^a-z0-9]", "", label.lower())  # "M2)@" == "M2) @"
                labels.setdefault(key, label)
                series[key][month_end(cy, cm).isoformat()] = (v, dp)
    if not series:
        return None
    # Published during the issue month; dated at its end (conservative: we only claim
    # the figure was public by then).
    return {
        "issue": f"{iy}-{im:02d}",
        "published": month_end(iy, im).isoformat(),
        "source": SOURCE,
        "freq": "monthly",
        "rows": [{"label": labels[k], "values": {d: list(v) for d, v in vals.items()}} for k, vals in series.items()],
    }


def parse(in_dir: str, out_json: str) -> None:
    issues = []
    for f in sorted(Path(in_dir).glob("IND_*.pdf")):
        try:
            iss = parse_issue(f)
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the run
            print(f"skip {f.name}: {exc}")
            continue
        if not iss:
            print(f"no indicator rows in {f.name}")
            continue
        # An issue prints the latest 1-3 months. A file whose newest column is far older
        # is a mis-filed upload (SBP's Dec-2005 page links Dec-2004's file): dating its
        # figures by the issue it is filed under would be wrong, so skip it.
        y, m = map(int, iss["issue"].split("-"))
        last = max(d for r in iss["rows"] for d in r["values"])
        lag = (y * 12 + m) - (int(last[:4]) * 12 + int(last[5:7]))
        if not 0 <= lag <= 6:
            print(f"skip {f.name}: newest column {last} is {lag} months from the issue (mis-filed?)")
            continue
        issues.append(iss)
    issues.sort(key=lambda i: i["issue"])
    Path(out_json).write_text(json.dumps(issues), encoding="utf-8")
    pts = sum(len(r["values"]) for i in issues for r in i["rows"])
    print(f"{len(issues)} issues, {pts} published values -> {out_json}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "download":
        download(sys.argv[2])
    elif cmd == "parse":
        parse(sys.argv[2], sys.argv[3])
    elif cmd == "apply":  # kept for the documented command line
        apply(sys.argv[2:3], commit="--commit" in sys.argv)
    else:
        print(__doc__)
