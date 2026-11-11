"""fbr_valuation_rulings - FBR customs valuation rulings: assessed customs values by item x origin, 2015 →.

Source: FBR's Directorate General of Customs Valuation determines the customs value of goods
under section 25A of the Customs Act, 1969 when declared values are unreliable. FBR lists every
ruling on https://www.fbr.gov.pk/showvaluations, a DataTables grid fed by
`POST /Home/LoadVALUATIONs` — the server answers "Value cannot be null. Parameter name: source"
unless the form carries DataTables' `columns[i][…]`, `order[0][…]` and `search[value]` fields
(`list_form`); one request with length=2000 returns all of them (947 on 2026-10-31, Jan 2015 →).
Each row: VALUATIONID, VALUATIONNumber (free text: "Valuation Ruling No. 2124/2026", "1647/2022",
"Public Notice 05/2023", blank), Title, CreationDate (/Date(ms)/, midnight Pakistan time) and the
PDF (download1.fbr.gov.pk/VALUATIONS/… or /Valuations/…; a few old links 404).

The PDFs are scanned letters. 2021 → mostly carry the scanner's own OCR text layer; most of
2016-2020 are bare images. Those, and text layers too garbled to yield a value (the scanner's OCR
reads "2124/2026" as "2I2q / 2026"), are OCR'd on GitHub Actions (`scripts.ocr_valuation_rulings`,
rapidocr — far cleaner) into `customs_valuation_tokens`, and parsed from there. Either way the parser reads word
boxes: it finds the value table by its header ("Origin" + a "US$/Kg"-style value column, "PCT"),
then pairs each value with the origin printed beside it, the nearest 8-digit PCT code (and the
12-digit WeBOC code where printed) and its block's description. A value row is kept only if it
passes every check: the value is a clean number, the origin is a country or "… Origins", the PCT
code is a code of the tariff, and the header named a currency and unit. `parse_status` says how a
ruling fared (parsed / partial: some values rejected / no_values / unreadable: no text and no
OCR yet / missing: the PDF 404s).

Claude transcriptions: the rulings the parser can't read (scans, garbled text layers, tables with
brand / size / per-origin columns) were read from their page images by Claude (2026-11) into
`backend/data/customs/valuation_rulings_claude.json`: per ruling the letter's date, the rulings it
supersedes and one row per value (item, variant, PCT and WeBOC codes, origin, value, unit, basis).
A ruling in that file takes its rows from it (`text_source` 'claude'), and every row still passes the
same checks: a positive value, a unit and an origin, an 8-digit code that is in the tariff, a WeBOC
code that extends it. The letter's own date ("Dated: 28.10.2026", often handwritten) comes only
from those readings — the scanner's OCR misreads it — else FBR's listing date stands. The letter's
printed number must be the ruling's listed number: in a stretch of FBR's 2017 list each entry links
the next ruling's PDF (listing 1036/2017 "Glass" opens letter 1038/2017), so a transcription is filed
under the listing whose number it prints (`scripts/claude_rulings/prepare.py merge`), and one that
still disagrees is not stored (`listed_number_typo` marks the few where FBR's list mistyped the number).

Dead links: some list entries point at a path FBR has since moved (`Valuations` vs `VALUATIONS`, the
year in the file name off by one, an unencoded comma); `pdf_candidates` tries those spellings.

Vintages: a ruling stays in force until a later one says "This Ruling supersedes Valuation Ruling
No. 2007/2025 dated …"; the superseded number is matched against FBR's list (the scanner's OCR
often reads "/" as "1": "200712025") and the earlier ruling gets superseded_by / superseded_on.
Rows: `customs_valuation_rulings` (all listed), `customs_valuation_values`. FBR times out from
the VPS and answers GitHub's runners → `customs.yml`.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from ingestion.framework import FetchedFile, IngestionJob

PAGE_URL = "https://www.fbr.gov.pk/showvaluations"
LIST_URL = "https://www.fbr.gov.pk/Home/LoadVALUATIONs"
_COLUMNS = ("VALUATIONNumber", "Title", "CreationDate", "UploadedFile1")
PKT = timezone(timedelta(hours=5))
PDF_BUDGET = 150          # PDFs read per run (the first run reads every ruling with --backfill)


def list_form(start: int = 0, length: int = 2000) -> dict:
    """The form the page's DataTables grid posts. Pure."""
    f = {"draw": "1", "start": str(start), "length": str(length), "search[value]": "", "search[regex]": "false",
         "order[0][column]": "0", "order[0][dir]": "asc", "department": ""}
    for i, c in enumerate(_COLUMNS):
        f.update({f"columns[{i}][data]": c, f"columns[{i}][name]": c, f"columns[{i}][searchable]": "true",
                  f"columns[{i}][orderable]": "true", f"columns[{i}][search][value]": "",
                  f"columns[{i}][search][regex]": "false"})
    return f


@dataclass
class Ruling:
    ruling_id: int
    number: str | None        # '2124/2026'
    number_raw: str | None
    kind: str                 # ruling | export_ruling | publication | withdrawal | public_notice | classification | other
    title: str
    item: str | None
    listed_on: date           # FBR's listing date (the ruling's own date can differ by a day or two)
    pdf_url: str


@dataclass
class ValueRow:
    line_no: int
    item: str
    pct_code: str | None      # 8 digits
    weboc_code: str | None    # the 12-digit WeBOC code where the ruling prints one ('701020001000')
    origin: str
    value: float
    currency: str
    unit: str
    basis: str | None


@dataclass
class ParsedRuling:
    ruling_id: int
    text_source: str | None   # 'text' | 'ocr' | None
    status: str               # parsed | partial | no_values | unreadable | missing
    supersedes: list[str] = field(default_factory=list)
    values: list[ValueRow] = field(default_factory=list)
    rejected: int = 0
    dated: date | None = None  # "Dated: 29.10.2026" on the letter


# ---- the list ---------------------------------------------------------------------------------

_NUM = re.compile(r"(\d{1,4})\s*(?:/|-|of)\s*(20\d\d|19\d\d)")
_ITEM = re.compile(r"determination\s+of\s+(?:the\s+)?customs\s*(?:/\s*)?(?:export\s+)?values?\s+(?:of|for)\s+(.+?)"
                   r"\s*(?:\(|under|u/s|in terms|section|$)", re.I)


def norm_number(raw: str | None) -> str | None:
    """'Valuation Ruling No. 2124/2026' / '1647-2022' / '1360 of 2019' -> '2124/2026'. Pure."""
    m = _NUM.search(raw or "")
    return f"{int(m.group(1))}/{m.group(2)}" if m else None


def kind_of(number_raw: str, title: str) -> str:
    t = f"{number_raw} {title}"
    if re.search(r"withdraw|rescind", title, re.I):
        return "withdrawal"
    if re.search(r"public\s+notice", t, re.I):
        return "public_notice"
    if re.search(r"classification|actual description", title, re.I):
        return "classification"
    if re.search(r"publication|value reference", number_raw, re.I):
        return "publication"
    if re.search(r"export", title, re.I):
        return "export_ruling"
    if re.search(r"determination|valuation|customs value", t, re.I):
        return "ruling"
    return "other"


def item_of(title: str) -> str | None:
    m = _ITEM.search(" ".join(title.split()))
    return m.group(1).strip(" ,.-") if m else None


def parse_list(payload: dict | str) -> list[Ruling]:
    """FBR's grid JSON -> one Ruling per row. Pure."""
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, dict) or "data" not in data:
        raise ValueError(f"unexpected valuation list response: {str(data)[:200]}")
    out = []
    for r in data["data"]:
        ms = int(re.search(r"-?\d+", r.get("CreationDate") or "").group())
        listed = datetime.fromtimestamp(ms / 1000, PKT).date()
        raw = " ".join((r.get("VALUATIONNumber") or "").split()) or None
        title = " ".join((r.get("Title") or "").split())
        url = (r.get("UploadedFile1") or "").strip()
        out.append(Ruling(int(r["VALUATIONID"]), norm_number(raw), raw, kind_of(raw or "", title), title,
                          item_of(title), listed, url))
    return out


# ---- word boxes -------------------------------------------------------------------------------

@dataclass
class Tok:
    x0: float
    top: float
    x1: float
    bottom: float
    text: str

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.top + self.bottom) / 2


def pdf_pages(content: bytes) -> list[list[Tok]]:
    """The PDF's own text layer as word boxes per page ([] pages when it has none)."""
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for p in pdf.pages:
            pages.append([Tok(w["x0"], w["top"], w["x1"], w["bottom"], w["text"])
                          for w in p.extract_words(x_tolerance=1.5, y_tolerance=2)])
            p.close()
    return pages


def ocr_pages(pages: list[dict]) -> list[list[Tok]]:
    """OCR tokens (a box per phrase: [[x,y]×4], text) split into word boxes, the width shared out
    by character count. Scaled to 72 dpi points so the parser's tolerances hold. Pure."""
    out = []
    for pg in pages:
        scale = 72 / float(pg.get("dpi") or 200)
        toks = []
        for t in pg.get("tokens") or []:
            xs = [p[0] for p in t["box"]]
            ys = [p[1] for p in t["box"]]
            x0, x1, y0, y1 = min(xs) * scale, max(xs) * scale, min(ys) * scale, max(ys) * scale
            words = t["text"].split()
            n = sum(len(w) for w in words) + max(len(words) - 1, 0)
            pos = 0
            for w in words:
                a = x0 + (x1 - x0) * pos / max(n, 1)
                b = x0 + (x1 - x0) * (pos + len(w)) / max(n, 1)
                toks.append(Tok(a, y0, b, y1, w))
                pos += len(w) + 1
        out.append(toks)
    return out


def lines_of(toks: list[Tok], tol: float = 4.0) -> list[list[Tok]]:
    lines: list[list[Tok]] = []
    for t in sorted(toks, key=lambda t: (t.yc, t.x0)):
        if lines and abs(t.yc - lines[-1][0].yc) <= tol:
            lines[-1].append(t)
        else:
            lines.append([t])
    return [sorted(ln, key=lambda t: t.x0) for ln in lines]


def text_of(pages: list[list[Tok]]) -> str:
    return "\n".join(" ".join(t.text for t in ln) for toks in pages for ln in lines_of(toks))


# ---- supersession -----------------------------------------------------------------------------

_SUPERSEDE = re.compile(r"supersed\w*\s+(?:the\s+)?(?:earlier\s+|previous\s+)?(?:valuation\s+)?(?:ruling|vr)s?"
                        r"\s*(?:no\.?|number|#)?\s*:?\s*(.{0,160})", re.I | re.S)
_GLUED = re.compile(r"\b(\d{2,4})[1l|I/7](20[12]\d)\b")
_SPLIT = re.compile(r"\b(\d{2,4})\s*[/\-|]\s*(20[12]\d)\b")


def supersedes_of(text: str, known: set[str] | None = None) -> list[str]:
    """Ruling numbers a ruling says it supersedes. The scanner OCR reads '2007/2025' as
    '200712025'; a glued reading counts only if it names a ruling in FBR's list (`known`). Pure."""
    out: list[str] = []
    for m in _SUPERSEDE.finditer(text):
        tail = re.split(r"\bdated\b|\n\s*\d\.|\bin\s+terms\b", m.group(1), maxsplit=1, flags=re.I)[0] + " "
        tail = re.sub(r"(?<=\d)[Oo](?=\d)", "0", tail)
        cands = [f"{int(a)}/{b}" for a, b in _SPLIT.findall(tail)]
        if not cands:
            cands = [f"{int(a)}/{b}" for a, b in _GLUED.findall(tail)]
            cands = [c for c in cands if known is None or c in known]
        for c in cands:
            if c not in out:
                out.append(c)
    return out


# ---- the value table --------------------------------------------------------------------------

_VALUE = re.compile(r"^\d{1,6}(?:,\d{3})*(?:\.\d{1,4})?$")
_CODE8 = re.compile(r"^(\d{4})\.(\d{4})$")
_CODE12 = re.compile(r"^(\d{4})\.(\d{4})\.(\d{4})$")
# "US$/Kg" (OCR also reads the dollar sign as "S" or "5": "USS/Kg", "US5/Kg")
_UNIT = re.compile(r"(?:US\s*[$S5]|USD|\$)\s*\)?\s*(?:/|per)?\s*\(?\s*(KGS?|KG|M\.?\s*TONS?|M\.?\s*T|MT|TONS?|UNITS?|PCS?|PIECES?|"
                   r"PAIRS?|SQ\.?\s*M(?:ETERS?|TRS?)?|SQM|M2|LITRES?|LITERS?|LTRS?|DOZEN|SETS?|NOS?\.?|EACH|METERS?|MTRS?)\b", re.I)
_UNITS = {"KG": "kg", "KGS": "kg", "MT": "mt", "M.T": "mt", "TON": "mt", "TONS": "mt", "UNIT": "unit", "UNITS": "unit",
          "PC": "pc", "PCS": "pc", "PIECE": "pc", "PIECES": "pc", "PAIR": "pair", "PAIRS": "pair", "SQM": "sqm",
          "M2": "sqm", "LITRE": "litre", "LITRES": "litre", "LITER": "litre", "LITERS": "litre", "LTR": "litre",
          "LTRS": "litre", "DOZEN": "dozen", "SET": "set", "SETS": "set", "NO": "unit", "NOS": "unit", "EACH": "unit",
          "METER": "m", "METERS": "m", "MTR": "m", "MTRS": "m"}
ORIGINS = {
    "afghanistan", "argentina", "australia", "austria", "bangladesh", "belgium", "brazil", "bulgaria", "cambodia",
    "canada", "chile", "china", "colombia", "czech republic", "denmark", "egypt", "ethiopia", "finland", "france",
    "germany", "greece", "hong kong", "hungary", "india", "indonesia", "iran", "iraq", "ireland", "israel", "italy",
    "japan", "jordan", "kazakhstan", "kenya", "korea", "south korea", "kuwait", "malaysia", "mexico", "morocco",
    "myanmar", "nepal", "netherlands", "new zealand", "nigeria", "norway", "oman", "pakistan", "philippines",
    "poland", "portugal", "qatar", "romania", "russia", "saudi arabia", "singapore", "slovakia", "south africa",
    "spain", "sri lanka", "sweden", "switzerland", "taiwan", "tanzania", "thailand", "tunisia", "turkey", "turkiye",
    "uae", "u.a.e", "united arab emirates", "united arab emirates uae", "united arab uae", "united arab", "dubai", "uk", "u.k", "united kingdom", "england", "ukraine", "usa",
    "u.s.a", "us", "united states", "america", "uzbekistan", "vietnam", "viet nam", "eu", "european union",
    "europe", "european", "asean", "far east", "middle east", "gcc", "africa", "latin america", "south america",
    "central asia", "scandinavia", "uganda", "ghana", "ivory coast", "sudan", "syria", "lebanon", "bahrain",
    "yemen", "azerbaijan", "belarus", "serbia", "croatia", "slovenia", "lithuania", "latvia", "estonia",
}
_ORIGIN_WORDS = re.compile(r"\borigins?\b|\bcountr(?:y|ies)\b|\bsources?\b", re.I)


def is_origin(text: str) -> bool:
    """A country / region or a list of them ('China, Taiwan, Indonesia'), or 'Other Origins' /
    'All Origins' / 'China & Other Origins'. Pure."""
    t = text.lower().replace("&", " and ")
    if not t.strip() or re.search(r"\d", t):
        return False
    if _ORIGIN_WORDS.search(t):
        # "Other Origins", "All Origin", "China & Other Origins" — not a bare "origins" fragment
        return bool(re.search(r"[a-z]", _ORIGIN_WORDS.sub(" ", t).replace("and", " ")))
    parts = [" ".join(re.sub(r"[()\[\].:*]", " ", p).split()) for p in re.split(r"\band\b|/|,|;|\bor\b", t)]
    parts = [p for p in parts if p]
    return bool(parts) and all(p in ORIGINS or p.replace(" ", "") in ORIGINS for p in parts)


def clean_origin(text: str) -> str:
    """The origin cell without WeBOC-code fragments that share its column ('Taiwan, 3920.4300.1 400
    Indonesia' -> 'Taiwan, Indonesia'). Pure."""
    words = [w for w in text.split() if not re.search(r"\d", w)]
    return " ".join(words).strip(" ,;/")


def _fix_digits(s: str) -> str:
    """Scanner OCR in digit runs: 'O'->'0', 'l'/'I'->'1'. Only applied to tokens that are otherwise
    digits and separators. Pure."""
    if re.fullmatch(r"[\dOoIl.,]+", s) and re.search(r"\d", s):
        return s.replace("O", "0").replace("o", "0").replace("I", "1").replace("l", "1")
    return s


def _header(lines: list[list[Tok]]) -> dict | None:
    """The value table's header: x of the origin and value columns, the first PCT column, the
    currency/unit. None when the page has none."""
    for i, ln in enumerate(lines):
        org = next((t for t in ln if re.fullmatch(r"origins?\.?|country", t.text, re.I)), None)
        if not org:
            continue
        # the header's lines around "Origin" — never a data line (one with a code, or a number
        # right of the origin column)
        band = [t for l2 in lines[max(0, i - 4):i + 5]
                if not any(_CODE8.match(_fix_digits(x.text)) or _CODE12.match(_fix_digits(x.text))
                           or (x.x0 > org.x0 and _VALUE.match(_fix_digits(x.text))) for x in l2)
                for t in l2 if abs(t.yc - org.yc) <= 26]
        text = " ".join(t.text for t in sorted(band, key=lambda t: (t.x0, t.top)))
        unit = _UNIT.search(" ".join(t.text for t in sorted(band, key=lambda t: (t.top, t.x0))))
        if not unit:
            # the unit stated just above the table ("… at the following Customs Values (US$/Kg):")
            near = [t for l2 in lines for t in l2 if org.yc - 90 <= t.yc < org.yc - 26]
            unit = _UNIT.search(" ".join(t.text for t in sorted(near, key=lambda t: (t.top, t.x0))))
            text = f"{text} {unit.group(0)}" if unit else text
        vals = [t for t in band if t.x0 > org.x0 and re.search(r"US|\$|C\s*&\s*F|C&F|value|FOB|CFR", t.text, re.I)]
        if not vals:
            continue
        pcts = [t for t in band if re.fullmatch(r"PCT|H\.?S\.?|HS\s*code|Heading", t.text, re.I)]
        return {
            "y": max(t.bottom for t in band),
            "origin_x0": org.x0 - 12,
            "value_x0": min(t.x0 for t in vals) - 12,
            "pct_x": min((t.x0 for t in pcts), default=None),
            "unit": _UNITS.get(re.sub(r"[\s.]", "", unit.group(1)).upper().replace("MTONS", "MT").replace("MTON", "MT"))
            if unit else None,
            "currency": "USD" if re.search(r"US\s*[$S5]\s*\)?\s*/|US\s*\$|USD|\$", text) else None,
            "basis": "C&F" if re.search(r"C\s*&\s*F|CFR", text, re.I) else ("FOB" if re.search(r"FOB", text) else None),
        }
    return None


_PROSE = re.compile(r"^(?:\d{1,2}\s*[.)]|\(?[ivx]{1,4}\))\s+[A-Za-z]|^(?:In\s+cases|Note|Explanation|The\s+(?:above|values))", re.I)
_SNO = re.compile(r"^(?:\d{1,3}|[Il]|[IVX]{1,4})[.)]?$")


def _table_end(lines: list[list[Tok]], start_y: float, value_x0: float) -> float:
    """y where the prose after the table starts ("8. In cases, where declared values …"): a
    numbered paragraph, a long line running across the columns with nothing in the value column,
    or the page footer ("Page 2 of 3")."""
    left = min((ln[0].x0 for ln in lines), default=0)
    for ln in lines:
        if ln[0].top <= start_y:
            continue
        text = " ".join(t.text for t in ln)
        prose = (len(ln) >= 10 and ln[0].x0 <= left + 40 and ln[-1].x1 > value_x0 - 60
                 and not any(_CODE8.match(t.text) or _CODE12.match(t.text) for t in ln)
                 and not any(t.x0 >= value_x0 and _VALUE.match(_fix_digits(t.text)) for t in ln))
        if (_PROSE.match(text) and len(ln) >= 5) or prose or re.search(r"(?i)\bpage\s*\d+\s*of\s*\d+", text):
            return ln[0].top - 1
    return 1e9


def _nearest(toks: list, y: float):
    return min(toks, key=lambda t: abs(t[0].yc - y)) if toks else None


def _blocks(anchors: list[float], y0: float, y1: float) -> list[tuple[float, float]]:
    """Row bands split half-way between consecutive anchors (S. No. cells). Pure."""
    if not anchors:
        return [(y0, y1)]
    a = sorted(anchors)
    cuts = [y0] + [(p + q) / 2 for p, q in zip(a, a[1:])] + [y1]
    return list(zip(cuts, cuts[1:]))


def parse_values(pages: list[list[Tok]], known_codes: set[str] | None = None,
                 debug: list | None = None) -> tuple[list[ValueRow], int, bool]:
    """Value rows from a ruling's word boxes: (rows passing every check, value cells rejected,
    whether a table header was found). `debug` collects the rejected cells. Pure.

    The table is cut into rows at its S. No. cells; within a row every value (one per origin) is
    paired with the origin printed beside it — or the row's / table's only origin when the origin
    is a merged cell — and with each 12-digit WeBOC code of the row (one value row per code), else
    its 8-digit PCT codes, else the nearest PCT code (a merged cell). The row's description, plus
    any brand / size cells beside the value, make the item."""
    rows: list[ValueRow] = []
    rejected = 0
    found = False
    hdr = None
    open_table = False         # the last page's table ran to the page's end

    def reject(*why):
        nonlocal rejected
        rejected += 1
        if debug is not None:
            debug.append(why)

    for toks in pages:
        lines = lines_of(toks)
        h = _header(lines)
        if h:
            hdr, top = h, h["y"]
        elif hdr and open_table:
            top = 0.0          # the table continues on the next page without a header
        else:
            continue
        found = True
        end = _table_end(lines, top, hdr["value_x0"])
        open_table = end >= 1e9
        body = [t for t in toks if top < t.top < end]
        if not body:
            continue
        vals = []
        for t in body:
            if t.x0 >= hdr["value_x0"]:
                s = _fix_digits(t.text.strip("*$"))
                if _VALUE.match(s):
                    vals.append((t, float(s.replace(",", ""))))
                elif re.search(r"\d", s):
                    reject("not a value", t.text)
        if not vals:
            continue
        c8 = [(t, m.group(1) + m.group(2)) for t in body if t.x0 < hdr["origin_x0"]
              and (m := _CODE8.match(_fix_digits(t.text)))]
        c12 = [(t, "".join(m.groups())) for t in body if t.x0 < hdr["origin_x0"]
               and (m := _CODE12.match(_fix_digits(t.text)))]
        code_left = min((t.x0 for t, _ in c8 + c12), default=hdr["pct_x"] or hdr["origin_x0"])
        code_right = max((t.x1 for t, _ in c8 + c12), default=code_left)
        sno = [t for t in body if t.x0 < code_left and t.x0 <= min(x.x0 for x in body) + 30 and _SNO.match(t.text)]
        desc = [t for t in body if t.x1 <= code_left + 2 and t not in sno]
        side = [t for t in body if code_right - 2 <= t.x0 < hdr["origin_x0"] and not _CODE8.match(t.text)
                and not _CODE12.match(t.text)]
        origin_toks = [t for t in body if hdr["origin_x0"] <= t.x0 < hdr["value_x0"]
                       and not _CODE8.match(t.text) and not _CODE12.match(t.text)]
        all_origins = {o for ln in lines_of(origin_toks, tol=9) if (o := clean_origin(" ".join(t.text for t in ln)))}

        def joined(ts):
            return " ".join(t.text for t in sorted(ts, key=lambda t: (round(t.top / 6), t.x0))).strip(" -:")

        lo_all, hi_all = top, max(t.bottom for t in body) + 1
        for b0, b1 in _blocks([t.yc for t in sno], lo_all, hi_all):
            bvals = sorted((v for v in vals if b0 <= v[0].yc < b1), key=lambda v: v[0].yc)
            if not bvals:
                continue
            item = joined([t for t in desc if b0 <= t.yc < b1])
            b12 = [c for c in c12 if b0 <= c[0].yc < b1]
            b8 = [c for c in c8 if b0 <= c[0].yc < b1]
            borg = [t for t in origin_toks if b0 <= t.yc < b1]
            for k, (vt, value) in enumerate(bvals):
                lo = (bvals[k - 1][0].yc + vt.yc) / 2 if k else b0
                hi = (bvals[k + 1][0].yc + vt.yc) / 2 if k + 1 < len(bvals) else b1
                origin = clean_origin(joined([t for t in borg if lo <= t.yc < hi]))
                if not origin and len(bvals) == 1 and borg:
                    origin = clean_origin(joined(borg))
                if not origin and len(all_origins) == 1:
                    origin = next(iter(all_origins))   # one merged origin cell for the whole table
                extra = joined([t for t in side if lo <= t.yc < hi])
                # one value for the row: every code of the row; several (one per origin or
                # grade): each value with the code printed nearest it
                if b12:
                    codes = (sorted({(c[1][:8], c[1]) for c in b12}) if len(bvals) == 1
                             else [(lambda c: (c[1][:8], c[1]))(_nearest(b12, vt.yc))])
                elif b8:
                    codes = (sorted({(c[1], None) for c in b8}) if len(bvals) == 1
                             else [(_nearest(b8, vt.yc)[1], None)])
                else:
                    near = _nearest(c8, vt.yc)
                    codes = [(near[1], None)] if near else [(None, None)]
                text = f"{item} ({extra})" if extra and extra not in item else item
                for code, weboc in codes:
                    ok = (value > 0 and is_origin(origin) and hdr["unit"] and hdr["currency"] and text
                          and (code is None or ((not known_codes or code in known_codes) and 1 <= int(code[:2]) <= 97)))
                    if not ok:
                        reject("row", value, origin, code, text[:60], hdr["unit"], hdr["currency"])
                        continue
                    rows.append(ValueRow(len(rows) + 1, text, code, weboc, " ".join(origin.split()), value,
                                         hdr["currency"], hdr["unit"], hdr["basis"]))
    return rows, rejected, found


def parse_ruling(ruling_id: int, pages: list[list[Tok]], source: str, known_numbers: set[str] | None = None,
                 known_codes: set[str] | None = None) -> ParsedRuling:
    """One ruling's word boxes -> supersession, value rows and status. Pure."""
    if sum(len(t.text) for p in pages for t in p) < 300:
        return ParsedRuling(ruling_id, None, "unreadable")
    text = text_of(pages)
    rows, rejected, found = parse_values(pages, known_codes)
    status = ("parsed" if rows and not rejected else "partial" if rows else "no_values")
    return ParsedRuling(ruling_id, source, status, supersedes_of(text, known_numbers), rows, rejected)


CLAUDE_FILE = "data/customs/valuation_rulings_claude.json"
_CODE8_DOTTED = re.compile(r"^\d{4}\.\d{4}$")
_CODE12_DOTTED = re.compile(r"^\d{4}\.\d{4}\.\d{4}$")


def load_claude(path: str | None = None) -> dict[int, dict]:
    """{ruling_id: transcription} from the repo's data file ({} when absent)."""
    import os

    p = path or os.path.join(os.path.dirname(__file__), "..", "..", CLAUDE_FILE)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return {int(k): v for k, v in json.load(f)["rulings"].items()}


def claude_ruling(ruling_id: int, t: dict, known_numbers: set[str] | None = None,
                  known_codes: set[str] | None = None, number: str | None = None) -> ParsedRuling:
    """A Claude transcription -> ParsedRuling, every row re-checked like a parsed one. `number` is the
    ruling's listed number: a letter that prints another number (FBR linked the wrong PDF) yields no
    values, unless the transcription says FBR's list mistyped it (`listed_number_typo`). Pure."""
    printed = norm_number(t.get("number_printed"))
    if number and printed and printed != number and not t.get("listed_number_typo"):
        return ParsedRuling(ruling_id, "claude", "no_values", [], [], len(t.get("rows") or []))
    rows: list[ValueRow] = []
    rejected = 0
    for r in t.get("rows") or []:
        value = r.get("value")
        unit = " ".join(str(r.get("unit") or "").lower().split())
        origin = " ".join(str(r.get("origin") or "").split())
        item = " ".join(str(r.get("item") or "").split())
        if r.get("variant"):
            item = f"{item} ({' '.join(str(r['variant']).split())})"
        codes = [c for c in (r.get("pct_codes") or []) if isinstance(c, str)]
        webocs = [c for c in (r.get("weboc_codes") or []) if isinstance(c, str) and _CODE12_DOTTED.match(c)]
        ok = (isinstance(value, (int, float)) and value > 0 and unit and len(unit) <= 24 and origin and item
              and (r.get("currency") or "USD"))
        pairs: list[tuple[str | None, str | None]] = []
        for c in codes:
            if not _CODE8_DOTTED.match(c):
                continue
            d8 = c.replace(".", "")
            mine = [w.replace(".", "") for w in webocs if w.replace(".", "").startswith(d8)]
            pairs += [(d8, w) for w in mine] or [(d8, None)]
        if not codes:
            pairs = [(None, None)]
        for code, weboc in pairs:
            good = ok and (code is None or ((not known_codes or code in known_codes) and 1 <= int(code[:2]) <= 97))
            if not good:
                rejected += 1
                continue
            rows.append(ValueRow(len(rows) + 1, item, code, weboc, origin, float(value),
                                 str(r.get("currency") or "USD"), unit, r.get("basis")))
        if not pairs:
            rejected += 1
    sup = [n for n in (norm_number(x) for x in t.get("supersedes") or []) if n and (not known_numbers or n in known_numbers)]
    dated = None
    if t.get("dated"):
        try:
            dated = date.fromisoformat(t["dated"])
        except ValueError:
            dated = None
    status = "parsed" if rows and not rejected else "partial" if rows else "no_values"
    return ParsedRuling(ruling_id, "claude", status, sup, rows, rejected, dated)


def pdf_candidates(url: str, number: str | None) -> list[str]:
    """The listed URL, then the spellings FBR has moved rulings to: the folder's case, a comma
    encoded, the year in valuationNNNN-YYYY.pdf off by one (ruling 795/2016 is valuation795-2015.pdf). Pure."""
    from urllib.parse import quote

    name = url.rsplit("/", 1)[-1]
    out = [quote(url, safe=":/?&=%#,"), quote(url, safe=":/?&=%#")]
    for d in ("VALUATIONS", "Valuations", "Docs"):
        out.append(f"https://download1.fbr.gov.pk/{d}/{quote(name, safe=',%')}")
    m = _NUM.search(number or "")
    if m and re.match(r"(?i)valuation\d", name):
        n, y = m.group(1), int(m.group(2))
        out += [f"https://download1.fbr.gov.pk/Valuations/valuation{n}-{yy}.pdf" for yy in (y, y - 1, y + 1)]
    seen: list[str] = []
    for u in out:
        if u not in seen:
            seen.append(u)
    return seen


def link_supersessions(rulings: list[Ruling], supersedes: dict[int, list[str]]) -> dict[int, tuple[int, date]]:
    """{superseded ruling_id: (superseding ruling_id, its issue date)}. A number listed twice (FBR
    re-used numbers across years only with a different year, so number/year is unique; duplicates
    are uploads of one ruling) maps to its latest listing before the superseding ruling. Pure."""
    by_num: dict[str, list[Ruling]] = {}
    for r in rulings:
        if r.number:
            by_num.setdefault(r.number, []).append(r)
    issued = {r.ruling_id: r.listed_on for r in rulings}
    out: dict[int, tuple[int, date]] = {}
    for rid, nums in supersedes.items():
        for n in nums:
            older = [r for r in by_num.get(n, []) if r.ruling_id != rid and r.listed_on <= issued[rid]]
            if older:
                old = max(older, key=lambda r: r.listed_on)
                cur = out.get(old.ruling_id)
                if cur is None or issued[rid] < cur[1]:
                    out[old.ruling_id] = (rid, issued[rid])
    return out


class FbrValuationRulingsJob(IngestionJob):
    name = "fbr_valuation_rulings"
    source = "FBR"

    def _list(self) -> bytes:
        from ingestion import http_client

        resp = http_client.post(LIST_URL, data=list_form(), timeout=120,
                                headers={"X-Requested-With": "XMLHttpRequest", "Referer": PAGE_URL})
        return resp.content

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        from app import db

        listing = self._list()
        rulings = parse_list(listing.decode("utf-8", "replace"))  # raises on a non-JSON answer
        state = {r["ruling_id"]: (r["parse_status"], r["text_source"]) for r in db.query(
            "SELECT ruling_id, parse_status, text_source FROM customs_valuation_rulings")}
        ocrd = {r["ruling_id"] for r in db.query("SELECT ruling_id FROM customs_valuation_tokens")}
        out = [FetchedFile("list.json", listing, date.today())]
        self._listed = {r.ruling_id: r.listed_on for r in rulings}
        self._listed_numbers = {r.ruling_id: r.number for r in rulings}
        claude = load_claude()
        n = 0
        for r in sorted(rulings, key=lambda r: -r.ruling_id):
            status, src = state.get(r.ruling_id, (None, None))
            if r.ruling_id in claude:
                if src != "claude":
                    out.append(FetchedFile(f"claude|{r.ruling_id}", b"", date.today()))
                continue           # Claude's reading of the page images replaces the parser's
            if status in ("parsed", "partial", "missing") or (status == "no_values" and src == "ocr"):
                continue           # read already
            if status in ("unreadable", "no_values"):
                # a scan, or a text layer too garbled to yield values: customs.yml OCRs it and the
                # next run parses the OCR tokens
                if r.ruling_id in ocrd:
                    out.append(FetchedFile(f"ocr|{r.ruling_id}", b"", date.today()))
                continue
            if n >= PDF_BUDGET and not backfill:
                break
            n += 1
            content = b"missing"
            for u in pdf_candidates(r.pdf_url, r.number_raw):
                try:
                    content = self.http_get(u)
                except Exception as exc:  # noqa: BLE001 — a dead link is that ruling's status, not the run's
                    content = f"missing: {exc}".encode()[:200]
                if content[:4] == b"%PDF":
                    break
            out.append(FetchedFile(f"pdf|{r.ruling_id}", content, r.listed_on))
        return out

    def parse(self, f: FetchedFile):
        from app import db

        if f.filename == "list.json":   # fetched first, so its numbers vet the supersession readings
            rulings = parse_list(f.content.decode("utf-8", "replace"))
            self._numbers = {r.number for r in rulings if r.number}
            return rulings
        kind, rid = f.filename.split("|")
        rid = int(rid)
        if kind == "claude":
            return [claude_ruling(rid, load_claude()[rid], self._numbers, self._codes,
                                  self._listed_numbers.get(rid))]
        if kind == "ocr":
            row = db.query_one("SELECT pages FROM customs_valuation_tokens WHERE ruling_id = %s", (rid,))
            pages, src = ocr_pages(row["pages"] if row else []), "ocr"
        elif f.content[:4] != b"%PDF":
            return [ParsedRuling(rid, None, "missing")]
        else:
            pages, src = pdf_pages(f.content), "text"
        return [parse_ruling(rid, pages, src, self._numbers, self._codes)]

    def run(self, backfill: bool = False) -> dict:
        from app import db

        self._numbers, self._listed, self._listed_numbers = None, {}, {}
        self._codes = {r["code"] for r in db.query("SELECT DISTINCT code FROM customs_tariff")} or None
        return super().run(backfill=backfill)

    def validate(self, records):
        rulings = [r for r in records if isinstance(r, Ruling)]
        if not rulings:
            raise ValueError("FBR's valuation list came back empty")
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        recs = [r for r, _ in validated]
        return upsert_all([r for r in recs if isinstance(r, Ruling)], [r for r in recs if isinstance(r, ParsedRuling)])


def upsert_all(rulings: list[Ruling], parsed: list[ParsedRuling]) -> int:
    """Rulings (metadata, every run), then each parsed ruling's values (replaced as a whole when
    the ruling is re-read), then the supersession links over everything stored."""
    from app import db

    known = {r.number for r in rulings if r.number}
    n = 0
    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO customs_valuation_rulings (ruling_id, number, number_raw, kind, title, item, listed_on, pdf_url)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (ruling_id) DO UPDATE SET number = EXCLUDED.number, number_raw = EXCLUDED.number_raw,
                   kind = EXCLUDED.kind, title = EXCLUDED.title, item = EXCLUDED.item,
                   listed_on = EXCLUDED.listed_on, pdf_url = EXCLUDED.pdf_url, updated_at = now()
               WHERE (customs_valuation_rulings.number, customs_valuation_rulings.number_raw,
                      customs_valuation_rulings.kind, customs_valuation_rulings.title, customs_valuation_rulings.item,
                      customs_valuation_rulings.listed_on, customs_valuation_rulings.pdf_url)
                     IS DISTINCT FROM (EXCLUDED.number, EXCLUDED.number_raw, EXCLUDED.kind, EXCLUDED.title,
                                       EXCLUDED.item, EXCLUDED.listed_on, EXCLUDED.pdf_url)""",
            [(r.ruling_id, r.number, r.number_raw, r.kind, r.title, r.item, r.listed_on, r.pdf_url) for r in rulings])
        n += cur.rowcount
        for p in parsed:
            sup = [s for s in p.supersedes if s in known]
            cur.execute(
                """UPDATE customs_valuation_rulings SET supersedes = %s, text_source = %s, parse_status = %s,
                       values_found = %s, dated = %s, updated_at = now() WHERE ruling_id = %s""",
                (sup or None, p.text_source, p.status, len(p.values), p.dated, p.ruling_id))
            cur.execute("DELETE FROM customs_valuation_values WHERE ruling_id = %s", (p.ruling_id,))
            cur.executemany(
                """INSERT INTO customs_valuation_values (ruling_id, line_no, item, pct_code, weboc_code, origin, value,
                       currency, unit, basis) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(p.ruling_id, v.line_no, v.item, v.pct_code, v.weboc_code, v.origin, v.value, v.currency, v.unit,
                  v.basis) for v in p.values])
            n += len(p.values)
        cur.execute("SELECT ruling_id, number, coalesce(dated, listed_on), supersedes FROM customs_valuation_rulings")
        stored = cur.fetchall()
        allr = [Ruling(rid, num, None, "", "", None, iss, "") for rid, num, iss, _ in stored]
        links = link_supersessions(allr, {rid: sup for rid, _, _, sup in stored if sup})
        cur.execute("UPDATE customs_valuation_rulings SET superseded_by = NULL, superseded_on = NULL "
                    "WHERE superseded_by IS NOT NULL AND NOT (ruling_id = ANY(%s))", (list(links),))
        cur.executemany(
            """UPDATE customs_valuation_rulings SET superseded_by = %s, superseded_on = %s
               WHERE ruling_id = %s AND (superseded_by, superseded_on) IS DISTINCT FROM (%s, %s)""",
            [(by, on, old, by, on) for old, (by, on) in links.items()])
    return n
