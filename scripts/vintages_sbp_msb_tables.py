"""Vintages from the SBP Monthly Statistical Bulletin's chapter TABLES (fiscal-year tables).

The indicators page (scripts/vintages_sbp_msb.py) is monthly. The bulletin's chapters also
print annual tables — national accounts (GNI by sector, GDP by expenditure, capital
formation, at current and constant prices) with fiscal-year columns marked R(evised) /
P(rovisional) — and every monthly issue reprints them. So each annual figure appears in
~12 issues a year, as provisional, revised and final: the point-in-time GDP history.

  python -m scripts.vintages_sbp_msb_tables download national_accounts IND_DIR OUT_DIR
      (IND_DIR holds MANIFEST.json from `vintages_sbp_msb download`; older issues come from
       archive.sbp.org.pk's per-issue index pages; issues without a named chapter fall back
       to the full bulletin PDF)
  python -m scripts.vintages_sbp_msb_tables parse OUT_DIR OUT.json
  python -m scripts.vintages_core apply MSB.json OUT.json [--commit]     (all sources together)

Rows become labels "<table title> | <row> | <price basis>" so the current- and constant-
price blocks (and each base year) are separate label families.
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path

from scripts.vintages_core import MONTHS, month_end
from scripts.vintages_sbp_msb import _PLACEHOLDER, _get, _to_number

SOURCE = "sbp_msb_na"
ARCHIVE_INDEX = "https://archive.sbp.org.pk/reports/stat_reviews/Bulletin/{y}/{m}/index.htm"
CHAPTERS = {
    # name -> (file-name pattern, a phrase the chapter's tables contain)
    "national_accounts": (re.compile(r"national[_-]?accounts|economic[_-]?growth", re.I),
                          re.compile(r"Gross National (Income|Product)|Expenditure on Gross Domestic Product", re.I)),
}


# ---- download ---------------------------------------------------------------------------

def download(kind: str, ind_dir: str, out_dir: str) -> None:
    name_re, content_re = CHAPTERS[kind]
    manifest = json.loads((Path(ind_dir) / "MANIFEST.json").read_text())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    issues = sorted(set(manifest) | {f"{y}-{m:02d}" for y in range(2011, 2020) for m in range(1, 13)})
    got = missing = 0
    for issue in issues:
        y, m = int(issue[:4]), int(issue[5:])
        f = out / f"{kind}_{y}_{MONTHS[m - 1]}.pdf"
        if f.exists() and f.stat().st_size:
            got += 1
            continue
        files = manifest.get(issue, {}).get("files", [])
        cands = [u for u in files if name_re.search(u) and u.lower().endswith(".pdf")]
        if not files:  # 2012-2018 are not on the new site's index: use the archive's issue page
            time.sleep(1.5)
            code, html = _get(ARCHIVE_INDEX.format(y=y, m=MONTHS[m - 1]))
            if code == 200:
                base = ARCHIVE_INDEX.format(y=y, m=MONTHS[m - 1]).rsplit("/", 1)[0] + "/"
                links = re.findall(r'href="([^"]+\.pdf)"', html.decode("utf-8", "replace"), re.I)
                cands = [base + h for h in links if name_re.search(h)]
        # renumbered 2024-26 chapters: fall back to the full bulletin
        cands += [u for u in files if u.lower().endswith(".pdf")
                  and re.search(r"complete|monthly_statistical_bulletin|/MSB_[A-Za-z]+_\d{4}\.pdf", u, re.I)]
        for u in cands:
            time.sleep(1.5)
            code, data = _get(u)
            if code == 200 and data[:4] == b"%PDF" and _has(data, content_re):
                f.write_bytes(data)
                got += 1
                break
        else:
            missing += 1
            print(f"  no {kind} chapter for {issue}")
    print(f"{got} {kind} chapters in {out}, {missing} missing")


def _has(data: bytes, content_re) -> bool:
    import pypdfium2 as pdfium

    try:
        return any(content_re.search(p.get_textpage().get_text_range()) for p in pdfium.PdfDocument(data))
    except Exception:  # noqa: BLE001
        return False


# ---- parse ------------------------------------------------------------------------------

_FY = re.compile(r"\b((?:19|20)\d{2})\s*-\s*(\d{2})\s*(?:\(?([RPF])\)?)?(?=\s|$)")
_TITLE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\s+([A-Z].*?)\s*$")
_NUM = re.compile(r"^\(?-?[\d,]*\.?\d+\)?$")


def _is_num(tok: str) -> bool:
    return bool(_NUM.match(tok)) or tok.lower() in _PLACEHOLDER


def fy_end(start: str) -> date:
    return date(int(start) + 1, 6, 30)


def header_fys(line: str) -> list[date] | None:
    """A header line is (almost) only fiscal-year tokens: '2014-15 2015-16 2016-17R ...'."""
    toks = list(_FY.finditer(line))
    if len(toks) < 2:
        return None
    rest = _FY.sub("", line)
    rest = re.sub(r"\b[RPF]\b", "", rest).strip(" |")
    if len(rest) > 4:
        return None
    return [fy_end(t.group(1)) for t in toks]


def bases(line: str) -> list[str]:
    """'At Current Price At Constant Basic Price of 2005-06' -> ['current prices',
    'constant prices 2005-06']."""
    out = []
    marks = list(re.finditer(r"\b(current|constant)\b", line, re.I))
    for i, m in enumerate(marks):
        if m.group(1).lower() == "current":
            out.append("current prices")
            continue
        seg = line[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(line)]
        base = re.search(r"((?:19|20)\d{2})\s*-\s*(\d{2})", seg)
        out.append(f"constant prices {base.group(1)}-{base.group(2)}" if base else "constant prices")
    return out


def parse_page(text: str) -> list[tuple[str, dict[str, tuple[float, int]]]]:
    rows: list[tuple[str, dict]] = []
    title, basis, cols = "", [], None
    pending: list[str] = []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        t = _TITLE.match(line)
        if t and not _is_num(line.split()[-1]):  # "11.1 Gross National Income" (a data row ends in numbers)
            title, basis, cols, pending = t.group(1), [], None, []
            continue
        b = bases(line)
        if b and not header_fys(line):
            basis = b
            continue
        fys = header_fys(line)
        if fys:
            cols, pending = fys, []
            continue
        if not cols:
            continue
        toks = line.split()
        vals: list[str] = []
        while toks and _is_num(toks[-1]):
            vals.insert(0, toks.pop())
        label = " ".join(toks)
        if len(vals) < len(cols):
            if not vals:
                pending.append(label)  # a wrapped label: numbers come on a later line
            else:
                pending = []
            continue
        label = " ".join(pending + [label]).strip() if pending else label
        pending = []
        if not label:
            continue
        vals = vals[-len(cols):]
        k = len(cols) // len(basis) if basis and len(cols) % len(basis) == 0 else len(cols)
        for j, (d, tok) in enumerate(zip(cols, vals)):
            v, dp = _to_number(tok)
            if v is None:
                continue
            blk = basis[j // k] if basis and len(cols) % len(basis) == 0 else (basis[0] if basis else "")
            full = " | ".join(x for x in (title, label, blk) if x)
            rows.append((full, {d.isoformat(): (v, dp)}))
    return rows


def parse_file(path: Path) -> dict | None:
    import pypdfium2 as pdfium

    m = re.search(r"_(\d{4})_(\w{3})\.pdf$", path.name)
    if not m:
        return None
    iy, im = int(m.group(1)), MONTHS.index(m.group(2).title()) + 1
    merged: dict[str, dict] = {}
    for page in pdfium.PdfDocument(str(path)):
        text = page.get_textpage().get_text_range()
        if not CHAPTERS["national_accounts"][1].search(text):
            continue
        for label, vals in parse_page(text):
            key = re.sub(r"[^a-z0-9|]", "", label.lower())
            merged.setdefault(key, {"label": label, "values": {}})["values"].update(
                {d: list(v) for d, v in vals.items()})
    if not merged:
        return None
    return {"issue": f"{iy}-{im:02d}", "published": month_end(iy, im).isoformat(),
            "source": SOURCE, "freq": "annual", "rows": list(merged.values())}


def parse(in_dir: str, out_json: str) -> None:
    issues = []
    for f in sorted(Path(in_dir).glob("*.pdf")):
        try:
            iss = parse_file(f)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {f.name}: {exc}")
            continue
        if not iss or len(iss["rows"]) < 30:
            # 2024+ bulletins print quarterly-growth tables under "FY26 / Q1 Q2 ..." headers
            # this parser doesn't read yet; a handful of stray rows means "layout not handled"
            print(f"no table rows in {f.name}" if not iss else f"skip {f.name}: layout not handled")
            continue
        # an issue prints fiscal years up to the latest completed one (or the one in progress)
        y = int(iss["issue"][:4])
        last = max(d for r in iss["rows"] for d in r["values"])
        if not (y - 4 <= int(last[:4]) <= y + 1):
            print(f"skip {f.name}: newest fiscal year {last} vs issue {iss['issue']} (mis-filed?)")
            continue
        issues.append(iss)
    issues.sort(key=lambda i: i["issue"])
    Path(out_json).write_text(json.dumps(issues), encoding="utf-8")
    print(f"{len(issues)} issues, {sum(len(r['values']) for i in issues for r in i['rows'])} values -> {out_json}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "download":
        download(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "parse":
        parse(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
