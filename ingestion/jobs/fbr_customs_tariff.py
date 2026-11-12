"""fbr_customs_tariff - Pakistan Customs Tariff: the duty on every 8-digit PCT code, FY2008-09 →.

Source: FBR's Pakistan Customs Tariff (the First Schedule to the Customs Act, 1969), one edition
per fiscal year, in force from 1 July:
    https://www.fbr.gov.pk/categ/laws/144                            FY2023-24 … current
    https://www.fbr.gov.pk/categ/customs-tariff/51149/70853/131190   "Previous year Customs Tariff",
                                                                     FY2008-09 … FY2021-22
FY2022-23 is linked from neither page; FBR's Budget 2022-23 site still hosts it
(https://www.fbr.gov.pk/Budget2022-23/CustomsSchedulesFinal/Pakistan-Customs-Tariff.pdf — a PDF
whose cross-reference table pdfminer can't read, so it is re-saved by PDFium first). `EDITIONS`
lists every file read. Each edition is a PDF printed from Excel or Word with a text layer — columns PCT
code | description | CD % (FY2009-10 also prints the pre-budget rate; the last column is the
year's) — except FY2013-14: an .xlsx of the tariff "as amended up to 30th June, 2013" that prints
the whole duty stack (unit, CD, RD / additional CD / anti-dumping, sales tax / FED, withholding
tax, concession SROs, every trade agreement's rate), plus an 8-page amendment schedule of the
2013-14 budget's changes, applied on top. FBR's other editions print customs duty only: RD, ACD
and sales tax are set by separate SROs, and the Fifth Schedule's concessions are another document.

Layout quirks, all read from word coordinates (pdftotext's layout mode misaligns codes and
descriptions in several editions):
  - a code line is `NNNN.NNNN  - - text  rate`; a heading line `NN.NN  text`; a line without a
    code is the hierarchy's text ("- Cattle:") or a wrapped continuation of the line above. The
    code column's x differs by edition (53 → 171 pt), so it is found per page; a code starting a
    wrapped description line sits at the text's x and is not a code line;
  - specific duties ("Rs.10550/MT" on edible oils, "Rs.250/set" on phones) are printed as
    quarter-turned glyphs: decoded column by column, or found at the end of the description when
    the cell starts left of the duty column; a wrapped one ("Rs. 5 per" / "meter") is joined;
  - a spaced font splits "25" into "2" "5" — joined when the gap is a hairline;
  - the rate sits on the code's first line, a point or two off in some editions.

Rows go to `customs_tariff` (one per code per fiscal year: fy = the year ending 30 June, so 2027 =
FY2026-27, in force from 1 July 2026; raw line in `description`, heading > parent lines in
`context`) and `customs_tariff_duties` (one per code, year and duty component). Each edition is a
vintage of the same code; a code split or merged between editions is kept as printed — it has no
row in the years it didn't exist. Mid-year SRO changes are not in the editions. FBR times out from
the VPS and answers GitHub's runners → `customs.yml`.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob

SOURCE_PAGE = "https://www.fbr.gov.pk/categ/laws/144"
ARCHIVE_PAGE = "https://www.fbr.gov.pk/categ/customs-tariff/51149/70853/131190"
D1 = "https://download1.fbr.gov.pk/Docs/"
# fiscal year (ending 30 June) -> the files of that edition as FBR links them, in the order they
# apply (FY2013-14: the tariff as amended to 30 June 2013, then the budget's amendment schedule).
# Chapters 98-99 are separate files up to FY2021-22.
EDITIONS: dict[int, list[str]] = {
    2009: [D1 + "20101181511231562TarrifChapter1-972008-2009.pdf", D1 + "201011815112410781TarrifChapter98-992008-2009.pdf"],
    2010: [D1 + "201011815111611546TarrifChapter1-972009-2010.pdf", D1 + "201011815111756593TarrifChapter98-99-2009-2010.pdf"],
    2011: [D1 + "20101181511936906TarrifChapter1-9720011.pdf", D1 + "201011815111325234TarrifChapter98-9920011.pdf"],
    2012: [D1 + "2011811128853223Tariff.pdf", D1 + "20118111283437914Chapter.pdf"],
    2013: [D1 + "201210916104244605CustomsTariff.pdf"],
    2014: [D1 + "2013811085627877CustomsTariff201314Web.xlsx", D1 + "20136171062622554CustomsTarrifBudget1314.pdf"],
    2015: [D1 + "20147211075037333PakistanCustomTarrifComplete.pdf"],
    2016: [D1 + "20157101374641856Reupdated5thScheduleandCUSTOMTariff.pdf"],
    2017: [D1 + "201610251510131910Tarrif-Chaper1To99-2016-17.pdf"],
    2018: [D1 + "2017112112112348253CustomsTariff2017-18Ch1-99.pdf"],
    2019: [D1 + "2018817158749999PakistanCustomTariff2018-19-CH1-99.pdf"],
    2020: [D1 + "20203201032341845PakistanCustomsTariff-19-20.pdf"],
    2021: [D1 + "202011613115526330CustomsTariff(Ch01-97).pdf", D1 + "20201161411157676Chapter-99.pdf"],
    2022: [D1 + "2021101313103646911PakistanCustomsTariff-Ch1-97.pdf", D1 + "2021101313103753401chapte-98&99.pdf"],
    # FY2022-23 is on neither tariff page: FBR's Budget 2022-23 site still hosts the final edition.
    2023: ["https://www.fbr.gov.pk/Budget2022-23/CustomsSchedulesFinal/Pakistan-Customs-Tariff.pdf"],
    2024: [D1 + "2023111715113124511PAKISTANCUSTOMSTARIFF-2023-24.pdf"],
    2025: [D1 + "20241021010287106PakistanCustomsTariff-2024-25.pdf"],
    2026: [D1 + "20258111683941732Tariff-2025-26.pdf"],
    2027: [D1 + "20268251181330180Tariff-2026-27.pdf"],
}
# Duty components (customs_tariff_duties.component). Trade-agreement rates are `fta:<agreement>`
# with FBR's own label ("fta:CN_FTA", "fta:SAFTA_NLDC[13]", "fta:LK_FTA (Concession %)" — a
# label saying "Concession %" is a margin of preference off the CD rate, not a rate).
COMPONENTS = {
    "cd": "Customs duty",
    "rd_acd": "Regulatory duty / additional customs duty / anti-dumping duty",
    "st": "Sales tax",
    "fed": "Federal excise duty",
    "wht": "Withholding income tax",
    "concession": "Concessions and exemptions (SRO references)",
    "rd": "Regulatory duty (the year's RD SRO; fbr_regulatory_duty)",
    "acd": "Additional customs duty (the year's ACD SRO; fbr_additional_customs_duty)",
}

CODE = re.compile(r"^\d{4}\.\d{4}$")
HEADING = re.compile(r"^\d{2}\.\d{2}$")
_DASHES = re.compile(r"^((?:-\s*)+)")
_RATE = re.compile(r"^\d{1,3}(?:\.\d+)?%?$")
_YEAR = re.compile(r"(20\d\d)\s*-\s*(\d\d)\b")
_SPECIFIC_IN_TEXT = re.compile(r"\s(Rs\.?\s*[\d,]+(?:\.\d+)?\s*(?:/|per\b)\s*\S.*)$", re.I)
_FTA_PAIR = re.compile(r"([^=;]+?)\s*=\s*([^;]+)")
# Repeated table headers ("PCT CODE  DESCRIPTION  CD%", "Heading / Sub-heading  Statistical suffix",
# "(1) (2) (3)") and the chapter / section notes some editions print between chapters.
_HEADER_LINE = re.compile(r"\bDESCRIPTION\b|\b[Ss]tatistical\b.*\b[Ss]uffix\b|^(?:\(\d\)\s*)+$|^(?:Heading|HEADING)\s*/")
_NOTES = re.compile(r"^(?:CHAPTER\s+\d+|SECTION\s+[IVXL]+\b|Section\s+Notes?|Chapter\s+Notes?|Notes?\s*[.:]?$|"
                    r"Sub-?\s*heading\s+Notes?|Supplementary\s+Notes?)", re.I)
# description text that ran into the duty column before the rate ("salts 10", ") 10", "& 5", "-6 20"):
# anything but a rate or a specific duty
_SPILL = re.compile(r"^((?!.*\bRs\b)(?!\d{1,3}(?:\.\d+)?%?$).*?(?:[A-Za-z)&(,;:/-]).*?)\s+(\d{1,3}(?:\.\d+)?%?)$")
_SPECIFIC = re.compile(r"Rs\.?\s*([\d,]+(?:\.\d+)?)", re.I)
_SPECIFIC_UNIT = re.compile(r"\b(MT|M\.T|Kg|KG|set|meter|metre|litre|liter|sq\.?\s*m|u|pc|pair|dozen|T)\b", re.I)


@dataclass
class TariffLine:
    fy: int                   # fiscal year ending 30 June (2027 = FY2026-27, in force from 1 July 2026)
    code: str                 # 8 digits, no dot ("01012100"), as trade_hs stores PCT codes
    description: str          # the line as printed, dashes included
    context: str              # heading > parent lines, as printed
    duties: dict = field(default_factory=dict)   # component -> (rate_pct | None, rate_text | None)
    unit: str | None = None   # unit of quantity, where the edition prints one (FY2013-14)
    source_url: str = ""
    part: int = 0             # which of the edition's files it came from (EDITIONS order)

    @property
    def cd(self) -> tuple[float | None, str | None]:
        return self.duties.get("cd", (None, None))


@dataclass
class _Line:
    y: float
    code: str | None          # 8-digit or heading code printed in the code column
    words: list = field(default_factory=list)   # description words (x0, text)
    duty: list = field(default_factory=list)    # duty-column words (x0, top, x1, text)

    @property
    def text(self) -> str:
        return " ".join(t for _, t in sorted(self.words)).strip()


def fiscal_year(fy: int) -> str:
    return f"{fy - 1}-{str(fy)[2:]}"


def in_force_from(fy: int) -> date:
    return date(fy - 1, 7, 1)


def parse_rate(text) -> tuple[float | None, str | None]:
    """A duty cell: 20 / '20' / '20%' -> (20.0, None); 'Free' -> (0.0, 'Free'); a specific duty in
    whatever order its rotated glyphs were read ('MT Rs.10800/', 'Rs.10800MT /') -> (None,
    'Rs.10800/MT'); anything else -> (None, text as printed). Pure."""
    if isinstance(text, (int, float)):
        return float(text), None
    t = " ".join((text or "").split()).rstrip(".").strip()
    if not t:
        return None, None
    if _RATE.match(t):
        return float(t.rstrip("%")), None
    if t.lower() in {"free", "nil", "exempt"}:
        return 0.0, t
    amt = _SPECIFIC.findall(t)
    rest = _SPECIFIC.sub(" ", t)
    unit = _SPECIFIC_UNIT.findall(rest)
    if len(amt) == 1 and len(unit) == 1 and not re.search(r"[A-Za-z]{2,}", _SPECIFIC_UNIT.sub(" ", rest).replace("per", "")):
        u = {"m.t": "MT", "t": "T", "kg": "Kg"}.get(unit[0].lower(), unit[0])
        return None, f"Rs.{amt[0]}/{u}"
    return None, t


def _level(text: str) -> int:
    m = _DASHES.match(text)
    return m.group(1).count("-") if m else 0


def _clean(text: str) -> str:
    return _DASHES.sub("", text).strip().rstrip(":").strip()


def group_lines(words: list[tuple], tol: float = 3.0) -> list[list[tuple]]:
    """Cluster (x0, top, x1, text) words into printed lines by their top. Pure."""
    lines: list[list[tuple]] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if lines and abs(w[1] - lines[-1][0][1]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    return lines


class Layout:
    """The duty column(s) of one edition, learnt from the first page that shows them."""

    HEADER = ("CD", "(%)", "PRE-BUDGET", "DUTY", "RATE")

    def __init__(self):
        self.duty_left: float | None = None    # x where the duty column(s) start

    def learn(self, lines: list[list[tuple]]) -> None:
        if self.duty_left is not None:
            return
        # The header ("CD (%)", "pre-budget CD% CD%", "2026-27 / CD (%)") is printed above the
        # first code line, right of "DESCRIPTION" — sometimes on the line below it.
        desc = next((x0 for ln in lines for x0, _, _, t in ln if "DESCRIPTION" in t.upper()), None)
        first_code = next((ln[0][1] for ln in lines if CODE.match(ln[0][3]) or HEADING.match(ln[0][3])), None)
        if desc is not None and first_code is not None:
            xs = [x0 for ln in lines if ln[0][1] < first_code for x0, _, _, t in ln
                  if x0 > desc and t.upper().startswith(self.HEADER)]
            if xs:
                self.duty_left = min(xs) - 25
                return
        # No header: the duty column is where short numeric tokens right of the text sit (the
        # text can run close to it, as in FY2013-14's amendment schedule).
        xs = sorted(x0 for ln in lines for x0, _, _, t in ln if _RATE.match(t) and x0 > 300)
        if len(xs) >= 5:
            self.duty_left = xs[len(xs) // 10] - 8


def _merge_spaced(ws: list[tuple], gap: float = 2.5) -> list[tuple]:
    """Join words printed with a hairline gap ('2' '5' -> '25' in a spaced font). Pure."""
    out: list[tuple] = []
    for w in ws:
        if out and w[0] - out[-1][2] < gap:
            p = out[-1]
            out[-1] = (p[0], p[1], w[2], p[3] + w[3])
        else:
            out.append(w)
    return out


def page_lines(words: list[tuple], layout: Layout) -> list[_Line]:
    """Turn a page's words into code / text lines with their duty-column words. Pure."""
    grouped = [sorted(ln) for ln in group_lines(words)]
    layout.learn(grouped)
    out: list[_Line] = []
    duty_left = layout.duty_left if layout.duty_left is not None else 10_000
    # The code column: where line-initial codes sit on this page.
    xs = sorted(ln[0][0] for ln in grouped if CODE.match(ln[0][3]))
    code_x = xs[len(xs) // 4] if xs else None
    for ln in grouped:
        code = None
        rest = ln
        first = ln[0]
        text = " ".join(w[3] for w in ln)
        if _HEADER_LINE.search(text) and not CODE.match(first[3]):
            continue      # a repeated table header
        if all(re.fullmatch(r"\d{1,3}", w[3]) for w in ln) and all(w[0] < duty_left for w in ln):
            continue      # a page number
        if (CODE.match(first[3]) or HEADING.match(first[3])) and first[0] < duty_left and (
                code_x is None or abs(first[0] - code_x) <= 12):
            code, rest = first[3], ln[1:]
        line = _Line(y=first[1], code=code)
        line.words = [(x0, t) for x0, _, _, t in rest if x0 < duty_left]
        line.duty = _merge_spaced([w for w in rest if w[0] >= duty_left])
        out.append(line)
    return out


def duty_cells(duty: list[tuple], gap: float = 15.0) -> list[str]:
    """The duty-column words split into cells (one per printed column), read line by line (a
    wrapped "Rs. 5 per" / "meter" stays in order). Pure."""
    cells: list[list[tuple]] = []
    for w in sorted(duty, key=lambda w: (round(w[1] / 4), w[0])):
        if cells and w[0] - cells[-1][-1][2] < gap:
            cells[-1].append(w)
        else:
            cells.append([w])
    return [" ".join(w[3] for w in c) for c in cells]


def rate_text(duty: list[tuple]) -> str:
    """The year's rate: the last (rightmost) column when every column is a plain rate
    (FY2009-10 prints "pre-budget CD%" before "CD%"); else the whole cell. Pure."""
    cells = duty_cells(duty)
    if len(cells) > 1 and all(_RATE.match(c.strip()) for c in cells):
        return cells[-1]
    return " ".join(cells)


def _attach_orphan_duty(lines: list[_Line]) -> None:
    """Duty words on a line with no code and no text: a wrapped specific duty ("Rs. 5 per" /
    "meter") joins the code line above; a rate printed a line away from its code goes to the
    nearest code line without one. Pure (mutates the lines)."""
    for i, ln in enumerate(lines):
        if not ln.duty or ln.code or ln.words:
            continue
        prev = lines[i - 1] if i else None
        if prev is not None and prev.code and prev.duty and parse_rate(rate_text(prev.duty))[0] is None and (
                ln.y - prev.y <= 16):
            prev.duty, ln.duty = prev.duty + ln.duty, []
            continue
        for j in (i - 1, i + 1):
            if 0 <= j < len(lines) and lines[j].code and CODE.match(lines[j].code) and not lines[j].duty and (
                    abs(lines[j].y - ln.y) <= 8):
                lines[j].duty, ln.duty = ln.duty, []
                break


def parse_lines(lines: list[_Line], fy: int, source_url: str = "") -> list[TariffLine]:
    """Walk the printed lines in order, tracking the heading and the dash hierarchy. Pure."""
    _attach_orphan_duty(lines)
    out: list[TariffLine] = []
    heading = ""
    stack: list[tuple[int, str]] = []     # (dash level, text) of the open parent lines
    last: TariffLine | None = None
    last_kind = None                      # what a continuation line extends: "heading" | "text" | "code"
    for ln in lines:
        text = ln.text
        if ln.code and HEADING.match(ln.code):
            heading, stack, last_kind = text, [], "heading"
            continue
        if ln.code:
            lvl = _level(text)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            ctx = " > ".join([h for h in [_clean(heading)] if h] + [_clean(t) for _, t in stack])
            cell = rate_text(ln.duty)
            spill = _SPILL.match(cell)
            if spill:
                # description words running into the duty column ("derivatives; salts 10")
                text, cell = f"{text} {spill.group(1)}".strip(), spill.group(2)
            if not cell:
                # a specific duty long enough to start inside the description column
                m = _SPECIFIC_IN_TEXT.search(text)
                if m:
                    cell, text = m.group(1), text[:m.start()].rstrip()
            rate = parse_rate(cell)
            last = TariffLine(fy, ln.code.replace(".", ""), text, ctx,
                              {"cd": rate} if rate != (None, None) else {}, None, source_url)
            out.append(last)
            stack.append((lvl, text))
            last_kind = "code"
            continue
        if not text:
            continue
        if _NOTES.match(text):
            heading, stack, last_kind = "", [], None   # chapter / section notes until the next heading
            continue
        if _level(text) == 0 and last_kind:
            # a wrapped continuation of the line above
            if last_kind == "heading":
                heading = f"{heading} {text}".strip()
            elif last_kind == "code" and last is not None:
                last.description = f"{last.description} {text}"
                if stack:
                    stack[-1] = (stack[-1][0], last.description)
            elif last_kind == "text" and stack:
                stack[-1] = (stack[-1][0], f"{stack[-1][1]} {text}")
            continue
        lvl = _level(text)
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, text))
        last_kind = "text"
    return out


def _rotated(c: dict) -> bool:
    m = c.get("matrix") or (1, 0, 0, 1)
    return abs(m[0]) < 0.5 and abs(m[1]) > 0.5


def decode_rotated(chars: list[dict]) -> list[tuple]:
    """Quarter-turned glyphs in a duty cell: each glyph column is a run of characters read bottom
    to top, and a cell's columns run left to right ("Rs." "105" "50/" "MT" -> "Rs.10550/MT").
    Returns one (x0, top, x1, text) word per cell. Pure."""
    runs: list[list[dict]] = []
    for c in sorted((c for c in chars if c["text"].strip()), key=lambda c: (round(c["x0"], 1), c["top"])):
        r = runs[-1] if runs else None
        if r and abs(r[-1]["x0"] - c["x0"]) < 1 and c["top"] - r[-1]["bottom"] < 1.5:
            r.append(c)
        else:
            runs.append([c])
    cells: list[list[list[dict]]] = []
    for r in sorted(runs, key=lambda r: (round(r[0]["top"]), r[0]["x0"])):
        cell = next((k for k in cells if abs(k[0][0]["top"] - r[0]["top"]) < 2.5 and r[0]["x0"] - k[-1][0]["x0"] < 20), None)
        if cell is None:
            cells.append([r])
        else:
            cell.append(r)
    out = []
    for cell in cells:
        cell.sort(key=lambda r: r[0]["x0"])
        text = "".join("".join(c["text"] for c in reversed(r)) for r in cell).strip()
        if text:
            out.append((cell[0][0]["x0"], cell[0][0]["top"], cell[-1][0]["x1"], text))
    return out


def repaired(content: bytes) -> bytes:
    """A PDF whose cross-reference table pdfminer can't read ("No /Root object" — FY2022-23's edition),
    re-saved by PDFium, which rebuilds it."""
    import pypdfium2

    out = io.BytesIO()
    pypdfium2.PdfDocument(content).save(out)
    return out.getvalue()


def pdf_words(content: bytes):
    """Yield each page's words as (x0, top, x1, text); rotated cells decoded into one word each."""
    import pdfplumber
    from pdfplumber.utils.exceptions import PdfminerException

    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except PdfminerException:
        pdf = pdfplumber.open(io.BytesIO(repaired(content)))
    with pdf:
        for page in pdf.pages:
            rot = [c for c in page.chars if _rotated(c)]
            flat = page.filter(lambda o: o.get("object_type") != "char" or not _rotated(o)) if rot else page
            words = [(w["x0"], w["top"], w["x1"], w["text"]) for w in flat.extract_words(x_tolerance=1.5, y_tolerance=2)]
            words += decode_rotated(rot)
            page.close()  # 600-page editions otherwise keep every page's objects in memory
            yield words


def parse_pdf(content: bytes, fy: int, source_url: str = "") -> list[TariffLine]:
    layout = Layout()
    lines: list[_Line] = []
    for words in pdf_words(content):
        lines += page_lines(words, layout)
    return parse_lines(lines, fy, source_url)


# FY2013-14 workbook columns, matched on the header row's text.
_XLSX_COLS = {"code": "PCT CODE", "description": "DESCRIPTION", "unit": "UOM", "cd": "CD",
              "rd_acd": "RD /", "st_fed": "ST / FED", "wht": "WHT", "concession": "CONCESSION", "fta": "FTAS"}


def _st_fed(cell) -> dict:
    """'16' -> st 16; '16   FED: 16% ad val and Rs.1/Kg' -> st 16 + fed '16% ad val and Rs.1/Kg'. Pure."""
    out = {}
    t = str(cell).strip() if cell is not None else ""
    if not t:
        return out
    m = re.match(r"^(\d{1,3}(?:\.\d+)?)\s*(?:%)?\s*(?:FED\s*:\s*(.+))?$", t, re.S)
    if m:
        out["st"] = (float(m.group(1)), None)
        if m.group(2):
            out["fed"] = parse_rate(m.group(2).strip())
    else:
        out["st"] = (None, " ".join(t.split()))
    return out


def fta_rates(cell) -> dict:
    """'CN_FTA=0; SAFTA_NLDC[13]=  5;  LK_FTA (Concession %) = 100%; ' -> {'fta:CN_FTA': (0.0, None),
    'fta:SAFTA_NLDC[13]': (5.0, None), 'fta:LK_FTA (Concession %)': (100.0, None)}. Pure."""
    out = {}
    for label, val in _FTA_PAIR.findall(str(cell or "")):
        label = " ".join(label.split())
        if label:
            out[f"fta:{label}"] = parse_rate(" ".join(val.split()))
    return out


def parse_xlsx(content: bytes, fy: int, source_url: str = "") -> list[TariffLine]:
    """FY2013-14's workbook (sheet "Schedule-print"): one row per printed line, every duty column."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb["Schedule-print"] if "Schedule-print" in wb.sheetnames else wb.worksheets[0]
    cols: dict[str, int] = {}
    lines: list[_Line] = []
    extra: dict[str, dict] = {}
    units: dict[str, str] = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if not cols:
            heads = [" ".join(str(c).upper().split()) if c is not None else "" for c in row]
            if any(h.startswith("PCT CODE") for h in heads) and any("DESCRIPTION" in h for h in heads):
                for key, label in _XLSX_COLS.items():
                    j = next((j for j, h in enumerate(heads) if h.startswith(label)), None)
                    if j is not None:
                        cols[key] = j
            continue

        def cell(key):
            j = cols.get(key)
            return row[j] if j is not None and j < len(row) else None

        code = str(cell("code") or "").strip()
        desc = " ".join(str(cell("description") or "").split())
        if not code and not desc:
            continue
        code = code if CODE.match(code) or HEADING.match(code) else None
        cd = parse_rate(cell("cd")) if code and CODE.match(code) else (None, None)
        lines.append(_Line(y=float(i), code=code, words=[(0.0, desc)] if desc else [],
                           duty=[(1.0, 0.0, 2.0, str(cell("cd")))] if cd != (None, None) else []))
        if code and CODE.match(code):
            k = code.replace(".", "")
            d: dict = {}
            rd = cell("rd_acd")
            if rd is not None and str(rd).strip():
                d["rd_acd"] = parse_rate(" ".join(str(rd).split()))
            d.update(_st_fed(cell("st_fed")))
            if cell("wht") is not None and str(cell("wht")).strip():
                d["wht"] = parse_rate(cell("wht"))
            con = cell("concession")
            if con is not None and str(con).strip():
                d["concession"] = (None, " ".join(str(con).split()))
            d.update(fta_rates(cell("fta")))
            extra[k] = d
            if cell("unit"):
                units[k] = str(cell("unit")).strip()
    rows = parse_lines(lines, fy, source_url)
    for r in rows:
        r.duties.update(extra.get(r.code, {}))
        r.unit = units.get(r.code)
    return rows


def parse_file(name: str, content: bytes, fy: int, source_url: str = "") -> list[TariffLine]:
    if content[:2] == b"PK":
        return parse_xlsx(content, fy, source_url)
    return parse_pdf(content, fy, source_url)


def merge_edition(files: list[list[TariffLine]]) -> list[TariffLine]:
    """One row per code for an edition read from several files, in the order they apply: within a
    file a code printed twice keeps its first printing with a rate (chapter 99 restating a code);
    a later file (FY2013-14's budget amendment) replaces the description and CD of the codes it
    lists, keeping the other components. Pure."""
    out: dict[str, TariffLine] = {}
    for i, rows in enumerate(files):
        seen: set[str] = set()
        for r in rows:
            prev = out.get(r.code)
            if prev is None:
                out[r.code] = r
            elif r.code in seen:
                if "cd" not in prev.duties and "cd" in r.duties:
                    out[r.code] = r
            elif i > 0 and "cd" in r.duties:
                prev.description, prev.source_url = r.description, r.source_url
                prev.context = prev.context or r.context
                prev.duties["cd"] = r.duties["cd"]
            seen.add(r.code)
    return list(out.values())


class FbrCustomsTariffJob(IngestionJob):
    name = "fbr_customs_tariff"
    source = "FBR"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        html = self.http_get(SOURCE_PAGE).decode("utf-8", "replace")
        listed = re.findall(r'href="(https://download1\.fbr\.gov\.pk/Docs/[^"]+)"[^>]*>([^<]*Tariff[^<]*)<', html, re.I)
        editions = {fy: list(urls) for fy, urls in EDITIONS.items()}
        for url, label in listed:  # an edition FBR links before EDITIONS knows it
            m = _YEAR.search(label) or _YEAR.search(url)
            if m and "fifth" not in label.lower() and int(m.group(1)) + 1 not in editions:
                editions[int(m.group(1)) + 1] = [url]
        if not listed:
            raise ValueError(f"no tariff links on {SOURCE_PAGE} — page layout changed?")
        years = sorted(editions) if backfill else sorted(editions)[-2:]
        return [FetchedFile(f"{fy}|{i}|{url}", self.http_get(url), date.today())
                for fy in years for i, url in enumerate(editions[fy])]

    def parse(self, f: FetchedFile):
        fy, i, url = f.filename.split("|", 2)
        rows = parse_file(url, f.content, int(fy), url)
        for r in rows:
            r.part = int(i)
        return rows

    def validate(self, records):
        by_fy: dict[int, dict[int, list[TariffLine]]] = {}
        for r in records:
            by_fy.setdefault(r.fy, {}).setdefault(r.part, []).append(r)
        out = []
        for fy, files in sorted(by_fy.items()):
            rows = merge_edition([files[i] for i in sorted(files)])
            # every edition has ~7,000 tariff lines with a rate; fewer means the layout broke
            if sum(1 for r in rows if "cd" in r.duties) < 6000:
                raise ValueError(f"FY{fiscal_year(fy)}: only {len(rows)} codes parsed")
            out += rows
        return [(r, False) for r in out]

    def upsert(self, validated) -> int:
        return upsert_rows([r for r, _ in validated])


def upsert_rows(rows: list[TariffLine]) -> int:
    """Idempotent: a row or duty is written only when it differs from what is stored; an
    edition's duties that are no longer printed for a code are removed. Bulk statements (psycopg
    pipelines executemany): the GHA runner writes over an SSH tunnel to the VPS, where one round
    trip per code (~140k on a backfill) would take hours."""
    from app import db

    n = 0
    with db.connection() as conn, conn.cursor() as cur:
        for fy in sorted({r.fy for r in rows}):
            mine = [r for r in rows if r.fy == fy]
            cur.executemany(
                """INSERT INTO customs_tariff (code, fy, description, context, unit, source_url)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (code, fy) DO UPDATE SET description = EXCLUDED.description,
                       context = EXCLUDED.context, unit = EXCLUDED.unit, source_url = EXCLUDED.source_url,
                       updated_at = now()
                   WHERE (customs_tariff.description, customs_tariff.context, customs_tariff.unit,
                          customs_tariff.source_url)
                         IS DISTINCT FROM (EXCLUDED.description, EXCLUDED.context, EXCLUDED.unit, EXCLUDED.source_url)""",
                [(r.code, r.fy, r.description, r.context, r.unit, r.source_url) for r in mine])
            n += max(cur.rowcount, 0)
            duties = [(r.code, r.fy, comp, pct, txt) for r in mine for comp, (pct, txt) in r.duties.items()]
            if duties:
                cur.executemany(
                    """INSERT INTO customs_tariff_duties (code, fy, component, rate_pct, rate_text)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (code, fy, component) DO UPDATE SET rate_pct = EXCLUDED.rate_pct,
                           rate_text = EXCLUDED.rate_text
                       WHERE (customs_tariff_duties.rate_pct, customs_tariff_duties.rate_text)
                             IS DISTINCT FROM (EXCLUDED.rate_pct, EXCLUDED.rate_text)""",
                    duties)
            # the edition's own components only: 'rd' and 'acd' belong to the SRO jobs
            cur.execute(
                """DELETE FROM customs_tariff_duties d
                   WHERE d.fy = %s AND d.code = ANY(%s) AND d.component NOT IN ('rd', 'acd')
                     AND NOT (d.code || '|' || d.component = ANY(%s))""",
                (fy, [r.code for r in mine], [f"{c}|{comp}" for c, _, comp, _, _ in duties]))
    return n
