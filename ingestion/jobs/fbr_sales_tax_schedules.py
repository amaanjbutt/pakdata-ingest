"""fbr_sales_tax_schedules - the Sales Tax Act's Sixth Schedule (exemptions) and Eighth Schedule (reduced
rates), one row per entry per edition, FY2009-10 →.

Source: FBR's Sales Tax Act, 1990 "as amended up to" a date (https://www.fbr.gov.pk/categ/sales-tax-act/301),
one edition per fiscal year: the one in force on 1 July, or the nearest after it, kept with the as-of date it
prints (FY2009-10: 28 Oct 2009; FY2016-17: 31 Aug 2016; FY2018-19: 9 Oct 2018 — FBR lists none for 1 July 2018,
and that edition survives only in the Internet Archive). `EDITIONS` lists every file read.

The Sixth Schedule (section 13) exempts goods: Table-1 imports or supplies, Table-2 local supplies only,
Table-3 imports of plant, machinery and inputs with conditions, Table-4 (2021 on) supplies of listed goods.
The Eighth Schedule (section 3(2)(aa)) sets reduced rates: Table-1 (rate and condition per entry) and, up to
FY2021-22, Table-2 (a 5% list). Each is a bordered table:
    S. No. | Description | Heading Nos. of the First Schedule to the Customs Act [| Rate | Condition]
whose columns are read from its header ("(1) (2) (3)…"), carried to the pages that don't repeat it. A
serial's own rows ("(i) Rotavator 8432.8010") are its items; a row that starts a page without a serial
continues the row the page before cut. Footnote markers ("807[", "811[0713.2010") are printed in a
smaller font and dropped; amendment brackets are removed; "***" / "omitted" entries are skipped. A cell
whose text is printed over another (struck-out and new wording overlaid) can't be read and is skipped.
Every edition is a text PDF except July 2014 (Word 97: cells and row ends come from the file's own
paragraph properties).

Rows go to `sales_tax_schedules` (the entry: description, codes and rate as printed, conditions) and
`sales_tax_schedule_codes` (each 8-digit code the entry covers: an 8-digit code printed and in that year's
customs tariff, or every tariff code under a printed heading such as "87.03" or "9937"). "Respective
headings" covers no code. FBR answers GitHub's runners → `customs.yml` with the tariff.
"""
from __future__ import annotations

import bisect
import io
import json
import logging
import os
import re
import struct
from collections import Counter
from dataclasses import dataclass
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob
from ingestion.jobs.fbr_customs_tariff import D1, fiscal_year

log = logging.getLogger(__name__)

SOURCE_PAGE = "https://www.fbr.gov.pk/categ/sales-tax-act/301"
WAYBACK = "https://web.archive.org/web/{}id_/http://download1.fbr.gov.pk:80/Docs/{}"
# fiscal year (ending 30 June) -> (the as-of date the edition prints, its file)
EDITIONS: dict[int, tuple[str, str]] = {
    2010: ("2009-10-28", D1 + "20101213131245110staxact1990.pdf"),
    2015: ("2014-07-01", D1 + "2014714137745112Sales-Act-Government-of-Pakistan-Updated-Up-to-July-2014.doc"),
    2016: ("2015-07-01", D1 + "201511212112651615ReupdatedSalesTaxActupdatedupto30062015amendedversion.pdf"),
    2017: ("2016-08-31", D1 + "201711317142724SALESTAXACT1990updatedupto31-8-2016.pdf"),
    2018: ("2017-07-01", D1 + "2017831184658713SALESTAXACT,1990Amededupto01.07.2017.pdf"),
    2019: ("2018-10-09", WAYBACK.format("20181110070933", "2018102415102314948SALESTAXACT1990amendedupto9thOctober2018.pdf")),
    2020: ("2019-06-30", D1 + "20191041410503505SalesTaxAct,1990(30.06.19)-bookversion.pdf"),
    2021: ("2020-06-30", D1 + "2020812168165722SalesTaxAct,1990updatedupto(30.06.2020)--.pdf"),
    2022: ("2021-06-30", D1 + "2021102913104021295SalesTaxAct,1990updatedupto30.06.2021withcontents.pdf"),
    2023: ("2022-06-30", D1 + "20227191074612108SalesTaxAct,1990withIndexupdatedupto30.06.2022.pdf"),
    2024: ("2023-06-30", D1 + "20237418746219SalesTaxAct,1990withIndexupdatedupto30.06.2023.pdf"),
    2025: ("2024-06-30", D1 + "20247231874252122SalesTaxAct,1990updatedbyFinanceAct,2024upto30.06.2024--12.07.2024.pdf"),
    2026: ("2025-06-30", D1 + "202586148252375SalesTaxActupdatedupto2025-26.pdf"),
    2027: ("2026-06-30", D1 + "20267171373418951SalesTaxAct1990updatedupto30.06.2026.pdf"),
}
SCHEDULES = {"sixth": "Sixth Schedule (exemptions)", "eighth": "Eighth Schedule (reduced rates)"}

_SCHED = re.compile(r"^[\W\d]*(?:THE\s+)?(SIXTH|SEVENTH|EIGHTH|EIGHT|NINTH|TENTH|ELEVENTH)\s+SCHEDULE\b")
_TABLE = re.compile(r"^[\W\d]*Table[\s-]*(\d+|[IVX]+)\b", re.I)
_FORM = re.compile(r"^[^A-Za-z]*Annex[\s-]*(?:[A-Z]|[IVX]+)\b")     # Annex-A, Annex-I: forms, not entries
_SERIAL = re.compile(r"^\d{1,3}[A-Z]{0,2}$")
_SUBLABEL = re.compile(r"^(?:\(?(?:[ivxlc]{1,6}|[A-Za-z]|\d{1,3})[).]|[-–•])", re.I)
_COLNUM = re.compile(r"^\(\s*\d+\s*\)$")
_OMITTED_JUNK = re.compile(r"(?i)omitted|[\s*.…()\[\]_\-–,;:]")
_ROLE_WORDS = (("serial", ("s. no", "s.no", "s no", "serial", "sr.")), ("desc", ("description",)),
               ("codes", ("heading", "pct", "hs code", "h.s.")), ("rate", ("rate",)), ("cond", ("condition",)))
_CODE8 = re.compile(r"(?<![\d.])(\d{4})\.\s?(\d{4})(?!\d|\.\d)|(?<![\d.])(\d{8})(?!\d|\.\d)")
_CODE6 = re.compile(r"(?<![\d.])(\d{4})\.(\d{2})(?!\d|\.\d)")
_HEAD = re.compile(r"(?<![\d.])(\d{2})\.(\d{2})(?!\d|\.\d)|(?<![\d.])(\d{4})(?!\d|\.\d)")
_RANGE = re.compile(r"(\d{4}\.\d{4})\s*(?:to|-|–)\s*(\d{4}\.\d{4})")
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
_PCT = re.compile(r"^(\d{1,2}(?:\.\d+)?)\s?%$")


@dataclass
class Item:
    """One printed row of an entry: the serial's own row, or one of its items."""
    desc: str = ""
    codes: str = ""
    rate: str = ""
    cond: str = ""


@dataclass
class Entry:
    fy: int
    as_of: str
    schedule: str          # 'sixth' | 'eighth'
    tbl: str               # '1', '2', '3', '4'
    seq: int               # position in its table (serials can repeat)
    serial: str
    items: list[Item]
    source_url: str = ""

    @property
    def description(self) -> str:
        return "\n".join(i.desc for i in self.items if i.desc)

    @property
    def codes_text(self) -> str | None:
        return "; ".join(i.codes for i in self.items if i.codes) or None

    @property
    def rate_text(self) -> str | None:
        rates = list(dict.fromkeys(i.rate for i in self.items if i.rate))
        return "; ".join(rates) or None

    @property
    def rate_pct(self) -> float | None:
        return rate_pct(self.rate_text)

    @property
    def condition(self) -> str | None:
        return " ".join(i.cond for i in self.items if i.cond) or None

    @property
    def item_list(self) -> list[dict] | None:
        if len(self.items) < 2:
            return None
        return [{"text": i.desc or None, "codes": i.codes or None, "rate": i.rate or None} for i in self.items]


@dataclass
class CodeRow:
    fy: int
    schedule: str
    tbl: str
    seq: int
    code: str              # 8 digits
    printed: str           # as printed: '0713.1000', '87.03', '9937'
    item: str | None       # the item's own text, when the code sits on one of an entry's items
    rate_text: str | None
    rate_pct: float | None


# ---- text helpers -----------------------------------------------------------------------------------

def clean(text: str | None) -> str:
    """Cell text without amendment brackets, quotes or control characters, wrapped lines joined. Pure."""
    if not text:
        return ""
    t = re.sub(r"\x13[^\x14\x15]*\x14?", "", text)            # Word field instructions
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x15]", "", t)
    t = re.sub(r"(?:(?<=^)|(?<=[\s,(]))\d{1,4}(?=\[)", "", t)   # a footnote number printed full size: "1[151."
    t = t.replace("\r", "\n").replace("\xa0", " ").replace("\t", " ")
    t = re.sub(r"[\[\]“”\"‖]", "", t)
    t = re.sub(r"-\n(?=\w)", "-", t)                            # "non-\ntoxic" -> "non-toxic"
    t = re.sub(r"\s*\n\s*", " ", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def norm_serial(text: str) -> str:
    t = re.sub(r"^[a-z]\s+(?=\d)", "", (text or "").strip())     # a stray mark before the number: "a 117."
    t = re.sub(r"[\s\[\]“”\"'.,)(\-–]", "", t)
    m = re.match(r"^0*(\d+)(.*)$", t)
    return (m.group(1) + m.group(2).upper()) if m else t


def _junk(item: Item) -> bool:
    return not _OMITTED_JUNK.sub("", item.desc) and not re.search(r"\d", item.codes)


def is_omitted(entry: Entry) -> bool:
    """'***', '***] omitted', '[……]', '(****)', 'Omitted.' — an entry the Act no longer has (items left
    printed under an omitted serial go with it). Pure."""
    main = entry.items[0]
    if main.desc and _junk(main):
        return True
    return all(_junk(i) for i in entry.items)


def rate_pct(text: str | None) -> float | None:
    """'5%', '[10%].', '10%.”; and' -> the percentage; anything else (two rates, Rs per kg) -> None. Pure."""
    if not text:
        return None
    t = re.sub(r"[\[\]]|\band\b", "", text).strip(" .;,”\"")
    m = _PCT.match(t)
    return float(m.group(1)) if m else None


def role_of(header_text: str) -> str | None:
    t = header_text.lower()
    for role, words in _ROLE_WORDS:
        if any(w in t for w in words):
            return role
    return None


def printed_codes(text: str) -> list[tuple[str, str]]:
    """Code tokens in a codes cell: [(kind, token)] with kind 'code' (8 digits), 'sub' (6 digits),
    'heading' (4 digits) or 'range' ('lo-hi'), in printed order. Pure."""
    # a code split by the text layer: "160 2.5000" -> "1602.5000"
    text = re.sub(r"(?<![\d.])(\d{1,3}) (\d{1,3})\.(\d{4})(?!\d)",
                  lambda m: f"{m.group(1)}{m.group(2)}.{m.group(3)}" if len(m.group(1) + m.group(2)) == 4 else m.group(0), text)
    out: list[tuple[int, str, str]] = []
    taken: list[tuple[int, int]] = []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    for m in _RANGE.finditer(text):
        out.append((m.start(), "range", m.group(1).replace(".", "") + "-" + m.group(2).replace(".", "")))
        taken.append(m.span())
    for m in _CODE8.finditer(text):
        if free(*m.span()):
            out.append((m.start(), "code", (m.group(1) + m.group(2)) if m.group(1) else m.group(3)))
            taken.append(m.span())
    for m in _CODE6.finditer(text):
        if free(*m.span()):
            out.append((m.start(), "sub", m.group(1) + m.group(2)))
            taken.append(m.span())
    for m in _HEAD.finditer(text):
        if free(*m.span()):
            tok = (m.group(1) + m.group(2)) if m.group(1) else m.group(3)
            before = text[max(0, m.start() - 12):m.start()].lower()
            after = text[m.end():m.end() + 4]
            if m.group(3) and 1947 <= int(tok) <= 2100 and (
                    re.search(r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\W*\d{0,2}\W*$|year\W*$|act\W*$", before)
                    or re.match(r"\s?-\s?\d", after)):
                continue   # "30 June, 2018", "Finance Act, 2019", "2018-19" printed in the codes column
            if m.group(3) and re.match(r"\s?(?:cc|kwh|kg|hp|mm|watt|kv)\b", text[m.end():m.end() + 6], re.I):
                continue
            part = re.match(r"\s?\((?:\d{1,2}|[ivx]{1,4}|[a-z])\)", text[m.end():m.end() + 6])
            if part:      # "9917(2)", "9908(i)": one entry of a Chapter 99 heading, not the whole heading
                out.append((m.start(), "part", tok + part.group(0).strip()))
                taken.append((m.start(), m.end() + part.end()))
                continue
            out.append((m.start(), "heading", tok))
            taken.append(m.span())
    return [(k, t) for _, k, t in sorted(out)]


# ---- readers: a file -> events ------------------------------------------------------------------------
# ("text", line) for text outside tables, ("header", roles) when an entries table's header is read,
# ("row", {role: text}, top_of_page, unreadable) for each table row.

def schedule_pages(content: bytes) -> tuple[int, int]:
    """0-based page range of the Sixth Schedule → the end of the Eighth (the Ninth's first page, or the
    end), found from each page's text. The contents pages name the schedules too: the start is the page
    that also cites section 13(1)."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(content)
    try:
        n = len(doc)
        start, end = None, n
        for i in range(n):
            tp = doc[i].get_textpage()
            t = tp.get_text_range()
            if start is None and re.search(r"SIXTH\s+SCHEDULE", t) and re.search(r"section\s*13\s*\(1\)|section\s*13\s*\]", t, re.I):
                start = i
            elif start is not None and i > start and re.search(r"(?:NINTH|TENTH)\s+SCHEDULE", t):
                end = i + 1
                break
        if start is None:
            raise ValueError("no Sixth Schedule found")
        return start, end
    finally:
        doc.close()


def _overlaps(chars: list[dict]) -> int:
    """Pairs of different characters printed over each other on one line (struck-out and new wording
    overlaid)."""
    n = 0
    cs = sorted(chars, key=lambda c: (round(c["top"]), c["x0"]))
    for a, b in zip(cs, cs[1:]):
        if abs(a["top"] - b["top"]) < 2 and b["x0"] < a["x1"] - 0.5 * (a["x1"] - a["x0"]) and a["text"] != b["text"] \
                and a["text"].strip() and b["text"].strip():
            n += 1
    return n


def _key(c: dict) -> tuple:
    return (round(c["x0"], 2), round(c["top"], 2), c["text"], round(c["size"], 2))


def _footnote_marks(chars: list[dict], small: float) -> set[tuple]:
    """Footnote numbers: runs of digits printed smaller than the body ("807[The", "811[0713.2010",
    "81013. ***"), followed by an amendment bracket or by body-size text. Other small text in a cell is
    the schedule's own ("[10%]" inserted in a smaller font, "[153.") and stays."""
    out: set[tuple] = set()
    i, n = 0, len(chars)
    while i < n:
        j = i
        while j < n and chars[j]["size"] < small and chars[j]["text"].isdigit():
            j += 1
        if j > i and (j == n or chars[j]["text"] == "[" or chars[j]["size"] >= small):
            out.update(_key(c) for c in chars[i:j])
        i = max(j, i + 1)
    return out


def _row_above(outside, tables, cols) -> tuple[float, dict] | None:
    """A page's first row printed without its top border (Sixth Schedule Table-4's list, 2021 on: "7
    Cucumbers and gherkins … 0707.0000" sits just above the drawn table): the lines from a number in
    the S. No. column down to the table, words placed in columns by where they sit."""
    if not tables or not cols:
        return None
    serial = next((c for c in cols if c[2] == "serial"), None)
    if serial is None:
        return None
    top = min(t.bbox[1] for t in tables)
    words = [w for w in outside.extract_words() if top - 50 < w["top"] and w["bottom"] <= top + 1]
    lines: dict[int, list] = {}
    for w in words:
        lines.setdefault(round(w["top"]), []).append(w)
    start = next((y for y in sorted(lines) if len(lines[y]) > 1 and (ws := sorted(lines[y], key=lambda w: w["x0"]))
                  and re.fullmatch(r"\d{1,3}[A-Z]?\.?", ws[0]["text"]) and serial[0] - 4 <= ws[0]["x0"] <= serial[1]), None)
    if start is None:
        return None
    out: dict[str, list[str]] = {}
    for y in sorted(k for k in lines if k >= start):
        for w in sorted(lines[y], key=lambda w: w["x0"]):
            mid = (w["x0"] + w["x1"]) / 2
            col = next((c for c in cols if c[0] - 2 <= mid <= c[1] + 2), None) \
                or min(cols, key=lambda c: min(abs(mid - c[0]), abs(mid - c[1])))
            out.setdefault(col[2], []).append(w["text"])
    return float(start), {k: clean(" ".join(v)) for k, v in out.items()}


def pdf_events(content: bytes, pages: tuple[int, int] | None = None) -> list[tuple]:
    import pdfplumber

    start, end = pages or schedule_pages(content)
    events: list[tuple] = []
    cols: list[list] | None = None      # [[x0, x1, role]] from the last header
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        body = None
        for pg in pdf.pages[start:end]:
            page = pg.dedupe_chars()
            tables = page.find_tables()
            if body is None:      # the body text size: the commonest size inside the tables
                sizes = Counter(round(c["size"]) for c in page.chars
                                for t in tables if t.bbox[0] <= c["x0"] <= t.bbox[2] and t.bbox[1] <= c["top"] <= t.bbox[3])
                body = sizes.most_common(1)[0][0] if sizes else None
            if body is None:
                continue
            drop = _footnote_marks(page.chars, 0.8 * body)
            flt = page.filter(lambda o: o.get("object_type") != "char" or _key(o) not in drop)
            tables = flt.find_tables()
            items: list[tuple[float, tuple]] = []
            boxes = [t.bbox for t in tables]
            # outside the tables: headings in the body size (the footnotes are printed smaller)
            outside = page.filter(lambda o: o.get("object_type") != "char" or (o.get("size", 0) >= 0.8 * body and not any(
                b[0] - 1 <= o["x0"] <= b[2] + 1 and b[1] - 1 <= o["top"] <= b[3] + 1 for b in boxes)))
            for ln in outside.extract_text_lines():
                if ln["text"].strip():
                    items.append((ln["top"], ("text", ln["text"].strip())))
            first = True
            lead = _row_above(outside, tables, cols)
            if lead:
                items.append((lead[0], ("row", lead[1], True, False)))
                first = False
            for t in tables:
                if max(len(r.cells) for r in t.rows) > 7 and not any(
                        "description" in (c or "").lower() and any("no" in (x or "").lower() for x in row)
                        for row in t.extract()[:2] for c in row):
                    continue     # a form (Annex-A, Annex-B …: a dozen columns), not entries
                texts = t.extract()
                for r, row in enumerate(t.rows):
                    cells = [(c, texts[r][k]) for k, c in enumerate(row.cells) if c is not None]
                    words = [clean(x) for _, x in cells]
                    if sum(1 for w in words if w) == 0:
                        continue
                    roles = [role_of(w) for w in words]
                    if "serial" in roles and "desc" in roles:
                        cols = []
                        for (box, _), role in zip(cells, roles):
                            if role:
                                cols.append([box[0], box[2], role])
                            elif cols:
                                cols[-1][1] = box[2]
                        items.append((row.bbox[1], ("header", [c[2] for c in cols])))
                        continue
                    if all(_COLNUM.match(w) for w in words if w):
                        continue
                    if cols is None:
                        continue
                    out: dict[str, list[str]] = {}
                    bad = False
                    for (box, raw), w in zip(cells, words):
                        if not w:
                            continue
                        mid = (box[0] + box[2]) / 2
                        col = next((c for c in cols if c[0] - 2 <= mid <= c[1] + 2), None) \
                            or min(cols, key=lambda c: min(abs(mid - c[0]), abs(mid - c[1])))
                        out.setdefault(col[2], []).append(w)
                        chars = [c for c in flt.chars if box[0] <= c["x0"] < box[2] and box[1] <= c["top"] < box[3]]
                        bad = bad or _overlaps(chars) >= 3
                    items.append((row.bbox[1], ("row", {k: " ".join(v) for k, v in out.items()}, first, bad)))
                    first = False
            events += [e for _, e in sorted(items, key=lambda x: x[0])]
            pg.close()
    return events


_OPSIZE = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}


def _para_flags(grpprl: bytes) -> tuple[bool, bool]:
    """(in a table, a row's end mark) from a paragraph's property modifiers (sprms)."""
    in_table = ttp = False
    i = 0
    while i + 2 <= len(grpprl):
        sprm = struct.unpack_from("<H", grpprl, i)[0]
        spra = sprm >> 13
        i += 2
        if spra == 6:
            if sprm == 0xD608:                                     # sprmTDefTable: a 2-byte size
                size = struct.unpack_from("<H", grpprl, i)[0] - 1
                i += 2
            elif sprm == 0xC615 and i < len(grpprl) and grpprl[i] == 255:
                break                                              # sprmPChgTabs, long form: nothing we need after
            else:
                size = grpprl[i] if i < len(grpprl) else 0
                i += 1
        else:
            size = _OPSIZE[spra]
        op = grpprl[i:i + size]
        if sprm == 0x2416 and op:                                  # sprmPFInTable
            in_table = op[0] == 1
        elif sprm in (0x2417, 0x244C) and op and op[0] == 1:       # sprmPFTtp / sprmPFInnerTtp
            ttp = True
        elif sprm == 0x6649 and len(op) == 4 and struct.unpack("<i", op)[0] > 0:   # sprmPItap
            in_table = True
        i += size
    return in_table, ttp


def doc_paragraphs(content: bytes) -> list[tuple[str, bool, bool]]:
    """A Word 97-2003 file's main text as paragraphs: [(text with its mark, in a table, row end mark)].
    The piece table gives the text; the paragraph properties (PlcBtePapx → FKP pages) say which paragraphs
    sit in a table cell ("\\x07" ends a cell) and which end a row."""
    from xlrd import compdoc

    cd = compdoc.CompDoc(content, logfile=open(os.devnull, "w"))
    wd = cd.get_named_stream("WordDocument")
    flags = struct.unpack_from("<H", wd, 0x0A)[0]
    tbl = cd.get_named_stream("1Table" if flags & 0x0200 else "0Table")
    csw = struct.unpack_from("<H", wd, 0x20)[0]
    lw = 0x22 + csw * 2 + 2
    cslw = struct.unpack_from("<H", wd, lw - 2)[0]
    ccp_text = struct.unpack_from("<i", wd, lw + 3 * 4)[0]
    rg = lw + cslw * 4 + 2

    def fclcb(k: int) -> tuple[int, int]:
        return struct.unpack_from("<Ii", wd, rg + k * 8)

    fc_clx, lcb_clx = fclcb(33)
    clx = tbl[fc_clx: fc_clx + lcb_clx]
    i = 0
    while clx[i] == 1:
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    lcb = struct.unpack_from("<I", clx, i + 1)[0]
    plc = clx[i + 5: i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    pieces: list[tuple[int, int, int, int]] = []
    text: list[str] = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, (n + 1) * 4 + k * 8 + 2)[0]
        a, b = cps[k], min(cps[k + 1], ccp_text)
        if a >= b:
            continue
        if fc & 0x40000000:
            o = (fc & ~0x40000000) // 2
            pieces.append((a, b, o, 1))
            text.append(wd[o: o + (b - a)].decode("cp1252", "replace"))
        else:
            pieces.append((a, b, fc, 2))
            text.append(wd[fc: fc + 2 * (b - a)].decode("utf-16-le", "replace"))
    txt = "".join(text)
    fc_bte, lcb_bte = fclcb(13)
    bte = tbl[fc_bte: fc_bte + lcb_bte]
    nb = (lcb_bte - 4) // 8
    runs: list[tuple[int, int, bool, bool]] = []
    for pn in struct.unpack_from(f"<{nb}I", bte, (nb + 1) * 4):
        page = wd[(pn & 0x3FFFFF) * 512: (pn & 0x3FFFFF) * 512 + 512]
        crun = page[511]
        rgfc = struct.unpack_from(f"<{crun + 1}I", page, 0)
        for j in range(crun):
            boff = page[(crun + 1) * 4 + j * 13] * 2
            props = (False, False)
            if boff:
                cb = page[boff]
                g = page[boff + 1: boff + 2 * cb] if cb else page[boff + 2: boff + 2 + 2 * page[boff + 1]]
                props = _para_flags(g[2:])
            runs.append((rgfc[j], rgfc[j + 1]) + props)
    runs.sort()
    starts = [r[0] for r in runs]

    def props_at(cp: int) -> tuple[bool, bool]:
        fc = next((f0 + (cp - a) * w for a, b, f0, w in pieces if a <= cp < b), -1)
        k = bisect.bisect_right(starts, fc) - 1
        return runs[k][2:] if k >= 0 and runs[k][0] <= fc < runs[k][1] else (False, False)

    out: list[tuple[str, bool, bool]] = []
    s = 0
    for cp, ch in enumerate(txt):
        if ch in "\r\x07":
            out.append((txt[s: cp + 1],) + props_at(cp))
            s = cp + 1
    if s < len(txt):
        out.append((txt[s:], False, False))
    return out


def doc_events(content: bytes) -> list[tuple]:
    paras = doc_paragraphs(content)
    # the schedule itself, not the contents page: from the Sixth Schedule heading that cites section 13(1)
    start = next((k for k in range(len(paras)) if "SIXTH SCHEDULE" in paras[k][0].upper() and any(
        re.search(r"section\s*13\s*\(1\)", p[0], re.I) for p in paras[k:k + 3])), None)
    if start is None:
        raise ValueError("no Sixth Schedule found")
    events: list[tuple] = []
    roles: list[str | None] | None = None
    cells: list[str] = []
    cell = ""
    for text, in_table, row_end in paras[start:]:
        if not in_table:
            cells, cell = [], ""     # a table that ended without a row mark (a form) leaves nothing behind
            if text.strip():
                events.append(("text", clean(text)))
            continue
        if row_end:
            words = [clean(c) for c in cells]
            cells = []
            if not any(words):
                continue
            rs = [role_of(w) for w in words]
            if "serial" in rs and "desc" in rs:
                roles = []
                for r in rs:
                    roles.append(r or (roles[-1] if roles else None))
                events.append(("header", [r for r in roles if r]))
                continue
            if all(_COLNUM.match(w) for w in words if w) or roles is None:
                continue
            out: dict[str, list[str]] = {}
            for k, w in enumerate(words):
                if w:
                    role = roles[k] if k < len(roles) else roles[-1]
                    out.setdefault(role, []).append(w)
            events.append(("row", {k: " ".join(v) for k, v in out.items()}, False, False))
            continue
        cell += text
        if text.endswith("\x07"):
            cells.append(cell[:-1])
            cell = ""
    return events


# ---- events -> entries --------------------------------------------------------------------------------

def parse_events(events: list[tuple], fy: int, as_of: str, source_url: str = "") -> tuple[list[Entry], Counter]:
    """The entries of the Sixth and Eighth Schedules, in printed order. Pure. Also counts what isn't stored."""
    out: list[Entry] = []
    skipped: Counter = Counter()
    schedule: str | None = None
    tbl = "1"
    in_form = False
    started = False
    entry: Entry | None = None
    bad = False
    seq: Counter = Counter()
    table_rate = ""    # a rate the Table prints above its entries ("… at the rate of five percent")

    def flush():
        nonlocal entry, bad
        if entry is not None:
            if entry.schedule == "eighth" and table_rate and not any(i.rate for i in entry.items):
                entry.items[0].rate = table_rate
            if bad:
                skipped["unreadable (overlaid text)"] += 1
                skipped[f"unreadable: {entry.schedule} T{entry.tbl} S.{entry.serial}"] += 1
            elif is_omitted(entry):
                skipped["omitted"] += 1
            elif not _SERIAL.match(entry.serial):
                skipped[f"serial not read: {entry.serial!r}"] += 1
            else:
                entry.items = entry.items[:1] + [i for i in entry.items[1:] if not _junk(i)]   # "[..]", "***" rows
                key = (entry.schedule, entry.tbl)
                seq[key] += 1
                entry.seq = seq[key]
                out.append(entry)
        entry, bad = None, False

    for ev in events:
        if ev[0] == "text":
            line = ev[1]
            m = _SCHED.match(line)
            if m and len(line) <= 40:
                flush()
                name = m.group(1)
                if name == "SIXTH":
                    schedule, started = "sixth", True
                elif name in ("EIGHTH", "EIGHT") and started:
                    schedule = "eighth"
                elif name in ("NINTH", "TENTH", "ELEVENTH") and started:
                    break
                else:
                    schedule = None
                tbl, in_form, table_rate = "1", False, ""
                continue
            m = _TABLE.match(line)
            if m and len(line) <= 60 and schedule:
                flush()
                t = m.group(1).upper()
                tbl = {"I": "1", "II": "2", "III": "3", "IV": "4"}.get(t, t)
                in_form, table_rate = False, ""
                continue
            m = re.search(r"at the rate of (\w+)\s*(?:per\s?cent|%)", line, re.I)
            if m and schedule == "eighth" and entry is None:
                n = _NUMBER_WORDS.get(m.group(1).lower(), m.group(1))
                table_rate = f"{n}%" if str(n).isdigit() else ""
                continue
            if _FORM.match(line) and len(line) <= 30:
                flush()
                in_form = True
            continue
        if ev[0] == "header":
            in_form = False
            continue
        cells, top, unreadable = ev[1], ev[2], ev[3]
        if not schedule or in_form:
            continue
        item = Item(cells.get("desc", ""), cells.get("codes", ""), cells.get("rate", ""), cells.get("cond", ""))
        serial = cells.get("serial", "").strip()
        if not norm_serial(serial):
            serial = ""      # a stray "(" in the S. No. column
        if serial:
            s = norm_serial(serial)
            lead = re.match(r"^\W*(\d{1,3}[A-Z]?)\.\s+\S", serial)
            if not _SERIAL.match(s) and lead:
                s = lead.group(1)        # the serial cell repeats the row's text: "11. Supply of ware potato …"
            if not _SERIAL.match(s) and entry is not None and _SUBLABEL.match(serial):
                # an item's label printed in the S. No. column: "(i)", "a)"
                item.desc = f"{serial} {item.desc}".strip()
                entry.items.append(item)
                bad = bad or unreadable
                continue
            flush()
            entry = Entry(fy, as_of, schedule, tbl, 0, s, [item], source_url)
            bad = unreadable
            continue
        if entry is None:
            skipped["row before any serial"] += 1
            continue
        m = re.match(r"^(\d{1,3})([A-Z]?)\.\s*(.*)$", item.desc)
        cur = re.match(r"^\d+", entry.serial)
        if m and cur and int(m.group(1)) == int(cur.group(0)) + 1 and not re.match(r"^1\.", entry.items[0].desc):
            # the next serial printed inside the description cell ("11. Supply of ware potato …", "30. ***")
            flush()
            item.desc = m.group(3)
            entry = Entry(fy, as_of, schedule, tbl, 0, m.group(1) + m.group(2), [item], source_url)
            bad = unreadable
            continue
        bad = bad or unreadable
        last = entry.items[-1]
        # a page's first row continues the row the page before cut when it starts mid-sentence (or has no
        # text, only codes); a new item starts with a capital or a label ("Dioctyl orthophthalates", "(xv) …")
        lower = item.desc[:1].islower() or item.desc[:1] in ",;:)" or not item.desc
        cont = (top and lower and not _SUBLABEL.match(item.desc)) or \
            (lower and item.desc and not item.codes and not item.rate and not _SUBLABEL.match(item.desc))
        if cont or not item.desc and not item.codes and (item.rate or item.cond):
            # the row the page before cut, or the rest of a wrapped line / a long condition
            last.desc = " ".join(x for x in (last.desc, item.desc) if x)
            last.codes = " ".join(x for x in (last.codes, item.codes) if x)
            last.rate = " ".join(x for x in (last.rate, item.rate) if x)
            last.cond = " ".join(x for x in (last.cond, item.cond) if x)
        else:
            entry.items.append(item)
    flush()
    return out, skipped


def parse_file(content: bytes, fy: int, as_of: str, source_url: str = "") -> tuple[list[Entry], Counter]:
    events = doc_events(content) if content[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" else pdf_events(content)
    return parse_events(events, fy, as_of, source_url)


# ---- entries -> codes -----------------------------------------------------------------------------------

def expand_codes(entries: list[Entry], tariff: dict[int, list[str]]) -> tuple[list[CodeRow], list[str]]:
    """Each entry's 8-digit codes: a printed code in that year's tariff, or every tariff code under a
    printed heading / 6-digit subheading / range. Without a tariff for the year, printed 8-digit codes
    only. Chapter 98-99 headings (the tariff's special-procedure chapters) and "9917(2)"-style references
    stay as printed. Returns (rows, rejects: printed codes the year's tariff doesn't have). Pure."""
    rows: list[CodeRow] = []
    rejects: list[str] = []
    for e in entries:
        codes = sorted(tariff.get(e.fy) or [])
        known = set(codes)
        seen: set[str] = set()
        rates = {i.rate for i in e.items if i.rate}
        entry_rate = next(iter(rates)) if len(rates) == 1 else (e.rate_text if rates else None)
        for idx, it in enumerate(e.items):
            item = it.desc if len(e.items) > 1 and it.desc else None
            rate = it.rate or entry_rate
            for kind, tok in printed_codes(it.codes):
                if kind == "part" or tok[:2] in ("98", "99"):
                    continue     # Chapters 98-99 (diplomats, gifts, temporary imports …): no product codes
                if kind == "code":
                    hits = [tok] if not known or tok in known else []
                    printed = f"{tok[:4]}.{tok[4:]}"
                elif not codes:
                    continue
                elif kind == "range":
                    lo, hi = tok.split("-")
                    hits = codes[bisect.bisect_left(codes, lo): bisect.bisect_right(codes, hi)]
                    printed = f"{lo[:4]}.{lo[4:]} to {hi[:4]}.{hi[4:]}"
                else:
                    hits = codes[bisect.bisect_left(codes, tok): bisect.bisect_left(codes, tok + "~")]
                    printed = f"{tok[:2]}.{tok[2:]}" if kind == "heading" else f"{tok[:4]}.{tok[4:]}"
                if not hits:
                    rejects.append(f"FY{fiscal_year(e.fy)} {e.schedule} T{e.tbl} S.{e.serial}: {printed}")
                for c in hits:
                    if c not in seen:
                        seen.add(c)
                        rows.append(CodeRow(e.fy, e.schedule, e.tbl, e.seq, c, printed, item, rate, rate_pct(rate)))
    return rows, rejects


# ---- the job --------------------------------------------------------------------------------------------

MIN_ENTRIES = {"sixth": 60, "eighth": 10}   # the fewest any edition prints: 78 (2009) and 13 (Jul 2014)


class FbrSalesTaxSchedulesJob(IngestionJob):
    name = "fbr_sales_tax_schedules"
    source = "FBR"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        years = sorted(EDITIONS) if backfill else sorted(EDITIONS)[-2:]
        self._fetched = years
        files = []
        for fy in years:
            as_of, url = EDITIONS[fy]
            files.append(FetchedFile(f"{fy}|{as_of}|{url}", self._get(url), date.today()))
        return files

    def _get(self, url: str) -> bytes:
        """The Internet Archive answers 429 to bursts: a few tries, spaced out."""
        import time

        last: Exception | None = None
        for attempt in range(4):
            try:
                body = self.http_get(url)
                if body[:5] == b"%PDF-" or body[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                    return body
                last = ValueError(f"{url}: not a PDF or Word file ({body[:40]!r})")
            except Exception as exc:  # noqa: BLE001 - retried, then raised
                last = exc
            time.sleep(10 * (attempt + 1))
        raise last or ValueError(url)

    def parse(self, f: FetchedFile):
        fy, as_of, url = f.filename.split("|", 2)
        entries, skipped = parse_file(f.content, int(fy), as_of, url)
        unread = [k.split(": ", 1)[1] for k in skipped if k.startswith(("unreadable: ", "serial not read: "))]
        log.info("%s FY%s: %d entries, %d omitted, not read: %s", self.name, fiscal_year(int(fy)), len(entries),
                 skipped["omitted"], ", ".join(unread) or "none")
        self._unread = getattr(self, "_unread", []) + [f"FY{fiscal_year(int(fy))} not read: {u}" for u in unread]
        return entries

    def validate(self, records):
        from app import db

        entries: list[Entry] = records
        by: Counter = Counter((e.fy, e.schedule) for e in entries)
        for fy in getattr(self, "_fetched", sorted({e.fy for e in entries})):
            for sch, least in MIN_ENTRIES.items():
                if fy == 2010 and sch == "eighth":
                    continue        # the 2009 edition has no Eighth Schedule
                if by[(fy, sch)] < least:
                    raise ValueError(f"FY{fiscal_year(fy)} {sch} schedule: only {by[(fy, sch)]} entries parsed")
        tariff: dict[int, list[str]] = {}
        for r in db.query("SELECT fy, code FROM customs_tariff WHERE fy = ANY(%(fys)s)",
                          {"fys": sorted({e.fy for e in entries})}):
            tariff.setdefault(r["fy"], []).append(r["code"])
        codes, rejects = expand_codes(entries, tariff)
        # printed codes the year's tariff doesn't have (mostly codes the Act never renumbered after an HS
        # revision) stay in codes_text only; entries that couldn't be read are reported, not guessed
        self.last_rejects = getattr(self, "_unread", []) + rejects
        log.info("%s: %d entries, %d codes; %d printed codes not in that year's tariff", self.name, len(entries),
                 len(codes), len(rejects))
        self._codes = codes
        return [(e, False) for e in entries]

    def upsert(self, validated) -> int:
        return upsert_rows([e for e, _ in validated], getattr(self, "_codes", []))


def _js(v) -> str | None:
    return json.dumps(v, sort_keys=True, ensure_ascii=False) if v else None


def upsert_rows(entries: list[Entry], codes: list[CodeRow]) -> int:
    """Replace each edition's rows as a whole (an edition is fixed once published; a re-read that differs
    replaces it), writing nothing when an edition is unchanged."""
    from app import db

    n = 0
    with db.connection() as conn, conn.cursor() as cur:
        for fy in sorted({e.fy for e in entries}):
            mine = [e for e in entries if e.fy == fy]
            mc = [c for c in codes if c.fy == fy]
            want_e = {(e.schedule, e.tbl, e.seq): (e.as_of, e.serial, e.description, e.codes_text, e.rate_text,
                                                   e.condition, _js(e.item_list))
                      for e in mine}
            want_c = {(c.schedule, c.tbl, c.seq, c.code): (c.printed, c.item, c.rate_text) for c in mc}
            cur.execute("""SELECT schedule, tbl, seq, as_of::text, serial, description, codes_text, rate_text, condition,
                                  items::text FROM sales_tax_schedules WHERE fy = %s""", (fy,))
            have_e = {tuple(x[:3]): (x[3], x[4], x[5], x[6], x[7], x[8], _js(json.loads(x[9]) if x[9] else None))
                      for x in cur.fetchall()}
            cur.execute("""SELECT schedule, tbl, seq, code, printed, item, rate_text FROM sales_tax_schedule_codes
                           WHERE fy = %s""", (fy,))
            have_c = {tuple(x[:4]): tuple(x[4:]) for x in cur.fetchall()}
            if have_e == want_e and have_c == want_c:
                continue
            cur.execute("DELETE FROM sales_tax_schedule_codes WHERE fy = %s", (fy,))
            cur.execute("DELETE FROM sales_tax_schedules WHERE fy = %s", (fy,))
            cur.executemany(
                """INSERT INTO sales_tax_schedules (fy, as_of, schedule, tbl, seq, serial, description, codes_text,
                       rate_text, rate_pct, condition, items, source_url)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(e.fy, e.as_of, e.schedule, e.tbl, e.seq, e.serial, e.description, e.codes_text, e.rate_text,
                  e.rate_pct, e.condition, json.dumps(e.item_list) if e.item_list else None, e.source_url)
                 for e in mine])
            cur.executemany(
                """INSERT INTO sales_tax_schedule_codes (fy, schedule, tbl, seq, code, printed, item, rate_text, rate_pct)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(c.fy, c.schedule, c.tbl, c.seq, c.code, c.printed, c.item, c.rate_text, c.rate_pct) for c in mc])
            n += len(mine) + len(mc)
    return n
