"""pbs_trade_hs - Pakistan's exports and imports by product code and partner country, FY1990-91 →.

Source: PBS "Exports/Imports by Commodities and Countries" — the full customs record, every
product line by every partner country, value in Rs thousand and quantity in the line's unit:

  FY1990-91 … FY2014-15   five-year "year-wise" PDFs (1_exp … 6_exp, 1_imp … 6_imp) and the
                          2013-14/2014-15 P6X/P6M PDFs
  FY2015-16 … FY2020-21   fixed-width text files (June + cumulative July-June, this year and last)
  FY2021-22 … FY2024-25   "D-10" files (same layout as PDF, then .xlsx)

The current files are linked from pbs.gov.pk/external-trade-statistics; the 1994-98 export
volume and the 2015-21 text files are no longer linked there, so they are read from the
Internet Archive's copies of PBS's own URLs (`ARCHIVED`). Product codes are 7-digit SITC-based
codes up to FY2001-02 and 8-digit Pakistan Customs Tariff (HS-based) codes from FY2002-03
(`classification`), so codes are comparable within, not across, the two eras. Each code has a
total line ('All countries') followed by one line per partner; the grand total is code 'TOTAL'.
Every file is reconciled on parse: each code's partner lines must sum to its total line.

Rows go to the dedicated `trade_hs` table (+ `trade_hs_codes`); a later file's figure for the same
year replaces an earlier one (PBS revises the previous year in the next file). PBS blocks the VPS,
so this runs on a workstation: `python -m scripts.load_trade_hs` (see that script).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from ingestion.framework import FetchedFile, IngestionJob

INDEX_URL = "https://www.pbs.gov.pk/external-trade-statistics/"
WP = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/"
_OLD = "http://www.pbs.gov.pk/sites/default/files//external_trade/8_digit_level/"
_OLD80 = "http://www.pbs.gov.pk:80/sites/default/files/external_trade/8_digit_level/"
_OLDS = "https://www.pbs.gov.pk/sites/default/files//external_trade/8_digit_level/"
# (wayback timestamp, original PBS URL) — files PBS no longer links.
ARCHIVED = [
    ("20160519093631", _OLD80 + "export/2_exp_%201994-95_to_1997-98.pdf"),
    ("20170205192758", _OLD + "import/6_imp_2013_to_2014.pdf"),
    ("20170205191408", _OLD + "import/EXPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%2C%20%282015-2016%29.txt"),
    ("20170205193215", _OLD + "import/IMPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%2C%20%282015-2016%29.txt"),
    ("20180128093521", _OLD80 + "export/EXPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES-PAKISTAN%20(2016-2017).txt"),
    ("20181114015742", _OLD + "import/IMPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES-%20PAKISTAN%20%282016-2017%29.txt"),
    ("20181113132040", _OLD80 + "export/EXPORTS%20BY%20COMMODITIES%20%20AND%20COUNTRIES-(2017-2018).txt"),
    ("20181114015915", _OLD + "import/IMPORTS%20BY%20COMMODITIES%20%20AND%20COUNTRIES-%282017-2018%29.txt"),
    ("20191009041918", _OLD80 + "export/EXPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%20(2018-2019).txt"),
    ("20191114062821", _OLD + "import/IMPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%20%282018-2019%29.P06M"),
    ("20201117084225", _OLD + "export/EXPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%20%282019-2020%29.txt"),
    ("20201117090547", _OLD + "export/IMPORTS%20BY%20COMMODITIES%20AND%20COUNTRIES%20%282019-2020%29.txt"),
    ("20211117165423", _OLDS + "export/EXPORT%20BY%20COMMODITIES%20AND%20COUNTRIES%202020-21.txt"),
    ("20211117171331", _OLDS + "import/IMPORT%20BY%20COMMODITIES%20AND%20COUNTRIES%202020-21.txt"),
]
_LINKED = re.compile(r"(?:\d_(?:exp|imp)_.*?\.pdf|D-10_(?:Export|Import)[-_0-9]*(?:-1)?\.(?:pdf|xlsx)|"
                     r"(?:Export|Import)-by-commodities-and-countries-[^\"']+\.pdf)$", re.I)

ALL = "All countries"
TOTAL = "TOTAL"
_YEAR = re.compile(r"^((?:19|20)\d\d)-((?:19|20)?\d\d)$")
_NUM = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_NIL = {"-", "--"}


@dataclass
class TradeHsRow:
    flow: str            # export | import | re-export
    fy: int              # fiscal year ending 30 June
    code: str
    country: str
    quantity: float | None
    value_pkr_th: float | None


@dataclass
class CodeMeta:
    code: str
    description: str
    unit: str


def classification(code: str) -> str:
    return "SITC" if len(code) == 7 else ("total" if code == TOTAL else "HS")


# PBS's partner spellings across 35 years ('U S A', 'U.S.America', 'BANGLA DESH', 'Hong Kong S.A.Re.Chi')
# → one name each. Places that really changed (Czechoslovakia, Yugoslavia, Serbia & Montenegro, the
# separate UAE emirates PBS listed until 2013) keep their own names. Keys: lower-case, spaces collapsed.
COUNTRY_ALIASES = {
    "u s a": "United States", "u.s.america": "United States", "fed rep of germany": "Germany",
    "germany, fed rep of": "Germany", "germany, dem rep of": "Germany (Dem. Rep.)", "bahrein": "Bahrain",
    "bangla desh": "Bangladesh", "bahama": "Bahamas", "belize(br. honduras)": "Belize",
    "b.indian ocean terr": "British Indian Ocean Territory", "bonaire saint & saba": "Bonaire, Sint Eustatius and Saba",
    "bosnia & herzegovina": "Bosnia and Herzegovina", "rep. of bosnia-her": "Bosnia and Herzegovina",
    "british virgin is": "British Virgin Islands", "brundi": "Burundi", "brunei darussalam": "Brunei",
    "burkina faso(fr.up.v": "Burkina Faso", "upper volta": "Burkina Faso", "burma": "Myanmar",
    "cambodia fr.kampuche": "Cambodia", "central african r": "Central African Republic",
    "central african rep": "Central African Republic", "cocos (keeling) is": "Cocos (Keeling) Islands",
    "comros": "Comoros", "congo, republic of": "Republic of the Congo", "rep of the congo": "Republic of the Congo",
    "co,te d,ivoire(fr.iv": "Cote d'Ivoire", "ivory coast": "Cote d'Ivoire", "czech republics": "Czech Republic",
    "d.r.of congo": "DR Congo", "zaire": "DR Congo", "el-salvador": "El Salvador", "egypt(u.a.r.)": "Egypt",
    "u a r (egypt)": "Egypt", "equatorial g.(ri.mi)": "Equatorial Guinea", "europien union": "European Union",
    "europeans union": "European Union", "falkland is.malvinas": "Falkland Islands", "fiji a br admn isls": "Fiji",
    "fr.yugoslav r.macedo": "North Macedonia", "macedonia": "North Macedonia", "fujrah": "Fujairah",
    "gibralter": "Gibraltar", "heard/mac donald is": "Heard and McDonald Islands", "hong kong s.a.re.chi": "Hong Kong",
    "iran ( islamic r.)": "Iran", "irish republic": "Ireland", "kazakstan": "Kazakhstan",
    "republic of kazakhst": "Kazakhstan", "korea d.p.republic": "North Korea", "korea, republic of": "South Korea",
    "kyrgyzstan/kyrgyz r": "Kyrgyzstan", "republic of kyrgyzst": "Kyrgyzstan", "lao people's d.r": "Laos",
    "leeward a windward i": "Leeward and Windward Islands", "leeward/windward is": "Leeward and Windward Islands",
    "libyan arab jamuhir": "Libya", "macao.china": "Macao", "malagasy": "Madagascar", "maldive islands": "Maldives",
    "mali rep": "Mali", "malta a gozo": "Malta", "micronesia,f.states": "Micronesia",
    "netherland antilles": "Netherlands Antilles", "new caledonia etc i": "New Caledonia",
    "new guinea a papua": "Papua New Guinea", "o.asia(tai.for.pe.ki": "Other Asia (Taiwan)",
    "palau(fr.pacific is)": "Palau", "portuguese guinea": "Guinea-Bissau", "ras-ul-khaimah": "Ras al-Khaimah",
    "rep. of croatia": "Croatia", "rep. of slovenia": "Slovenia", "republic of armenia": "Armenia",
    "republic of azerbaij": "Azerbaijan", "republic of belarus": "Belarus", "republic of benin": "Benin",
    "republic of estonia": "Estonia", "republic of georgia": "Georgia", "republic of latvia": "Latvia",
    "republic of lithuani": "Lithuania", "republic of maldova": "Moldova", "republic of moldova": "Moldova",
    "republic of tajikist": "Tajikistan", "republic of turkmeni": "Turkmenistan", "republic of ukraine": "Ukraine",
    "republic of uzbekist": "Uzbekistan", "republic of yemen": "Yemen", "yemen dem. rep of": "Yemen (Dem. Rep.)",
    "reunion islands": "Reunion", "rio de ore": "Western Sahara", "western sahara(ri.de": "Western Sahara",
    "rio mini a other spa": "Rio Muni and other Spanish Africa", "ruanda": "Rwanda", "ryukus": "Ryukyu Islands",
    "russian federation": "Russia", "s.georgia s.sandw.is": "South Georgia and South Sandwich Islands",
    "saint martin(dutch p": "Sint Maarten", "saint martin(dutch po)": "Sint Maarten",
    "saint martin(french p)": "Saint Martin", "serbia & montenegro": "Serbia and Montenegro",
    "yug.f.r.(serb/monten": "Serbia and Montenegro", "slovakia/slovak r": "Slovakia", "solovakia republics": "Slovakia",
    "south africa rep": "South Africa", "sri lunka": "Sri Lanka", "st. helena": "Saint Helena",
    "st.kitts and nevis": "Saint Kitts and Nevis", "st. lucia": "Saint Lucia",
    "st.pierre & miquelon": "Saint Pierre and Miquelon", "st.vincent/grenadine": "Saint Vincent and the Grenadines",
    "sultanate of oman": "Oman", "surinam": "Suriname", "svalbard/jan may.is": "Svalbard and Jan Mayen",
    "swaziland": "Eswatini", "syrian arab republic": "Syria", "u.r.of tanzania": "Tanzania",
    "trinidad a tobago": "Trinidad and Tobago", "turks & caicos is": "Turks and Caicos Islands",
    "us.minor outlying is": "US Minor Outlying Islands", "us.virgin islands": "US Virgin Islands",
    "venezuela,bolivar. r": "Venezuela", "viet nam": "Vietnam", "wallis & futuna is": "Wallis and Futuna",
    "western samoa": "Samoa", "east timor": "Timor-Leste", "timor leste": "Timor-Leste", "aland island": "Aland Islands",
    "all other origin": "Other (origin not specified)", "christmas islands": "Christmas Island",
    "northern mariana is": "Northern Mariana Islands", "faeroe islands": "Faroe Islands",
    "french southern terr": "French Southern Territories", "um-ul-qaiwan": "Umm al-Quwain",
    "sao tome & principe": "Sao Tome and Principe", "antigua and barbuda": "Antigua and Barbuda",
    "african countries ns": "Africa (not specified)", "asian countries n s": "Asia (not specified)",
    "central america n s": "Central America (not specified)", "eastern europe n s": "Eastern Europe (not specified)",
    "middle east n.s": "Middle East (not specified)", "north america n s": "North America (not specified)",
    "oceania n.s": "Oceania (not specified)", "south america ns": "South America (not specified)",
    "western europe n s": "Western Europe (not specified)", "gaza strip": "Gaza Strip", "holy see": "Holy See",
}
_SMALL = {"and", "of", "the"}


def clean_country(name: str) -> str:
    """A partner label → its canonical name (see COUNTRY_ALIASES); 1990s ALL-CAPS labels title-cased. Pure."""
    s = re.sub(r"\s+", " ", name).strip(" .")
    alias = COUNTRY_ALIASES.get(s.lower())
    if alias:
        return alias
    if s.isupper():
        s = " ".join(w.lower() if w.lower() in _SMALL and i else w.capitalize() for i, w in enumerate(s.split()))
    return s


# ---- line sources: every format becomes lines of (x0, x1, text) words, top to bottom -------------

def pdf_lines(content: bytes):
    """PDF words grouped into lines. PyMuPDF when installed (fast; workstation backfills only —
    it is not in the image), else pdfplumber."""
    try:
        import pymupdf
    except ImportError:
        pymupdf = None
    if pymupdf is not None:
        pages = ([(w[0], w[1], w[2], w[4]) for w in p.get_text("words")]
                 for p in pymupdf.open(stream=content, filetype="pdf"))
    else:
        import pdfplumber

        pdf = pdfplumber.open(io.BytesIO(content))
        pages = ([(w["x0"], w["top"], w["x1"], w["text"]) for w in p.extract_words()] for p in pdf.pages)
    for words in pages:
        # cluster by baseline: a value can sit a point off its label (the D-10 PDFs)
        line: list = []
        y_line = None
        for x0, y0, x1, t in sorted(words, key=lambda w: (w[1], w[0])):
            if y_line is not None and y0 - y_line > 2.0:
                yield sorted(line)
                line = []
            if not line:
                y_line = y0
            line.append((x0, x1, t))
        if line:
            yield sorted(line)


def text_lines(content: bytes):
    """Fixed-width text: a word's x is its character column (scaled to PDF-like points). Pure."""
    for line in content.decode("latin-1").splitlines():
        yield [(m.start() * 6.0, m.end() * 6.0, m.group()) for m in re.finditer(r"\S+", line)]


def _flow_of(text: str, current: str) -> str:
    u = re.sub(r"\s*-\s*", "-", text.upper())  # 'RE - EXPORTS' in the 1990s volumes
    if "COMMODIT" in u and "COUNTR" in u and "BY" in u:
        for key, flow in (("RE-EXPORT", "re-export"), ("RE-IMPORT", "re-import"), ("IMPORT", "import"),
                          ("EXPORT", "export")):
            if key in u:
                return flow
    return current


def parse_lines(lines, flow: str) -> tuple[list[TradeHsRow], dict[str, CodeMeta]]:
    """Generic parser for the year-wise and the June/cumulative layouts. Pure.

    Value columns come from the header row of QUANTITY / VALUE (or RUPEES) words: each number is
    assigned to the column whose right edge is nearest its own right edge. Fiscal years come from
    'YYYY-YYYY' headers (year-wise: one per column pair) or, in the June/cumulative layout, from the
    cumulative pairs (this year, then last year) with the year read from the 'JUN YYYY' / spaced
    '2 0 1 5 - 2 0 1 6' header."""
    rows: list[TradeHsRow] = []
    meta: dict[str, CodeMeta] = {}
    edges: list[tuple[float, float]] | None = None
    pair_fy: list[int | None] = []
    years: list[int] = []
    cum_x: list[float] = []
    head_years: list[int] = []
    code: str | None = None
    unit = ""
    for ws in lines:
        if not ws:
            continue
        texts = [w[2] for w in ws]
        joined = " ".join(texts)
        flow = _flow_of(joined, flow)
        up = [t.upper() for t in texts]
        yrs = [w for w in ws if _YEAR.match(w[2])]
        if len(yrs) >= 2 and not any(_NUM.match(t) and len(t) > 4 for t in texts):
            years = [int(_YEAR.match(w[2]).group(1)) + 1 for w in yrs]
            continue
        spaced = re.findall(r"((?:\d ){3}\d) - ((?:\d ){3}\d)", joined)
        if spaced:
            head_years = [int(b.replace(" ", "")) for _, b in spaced]
            continue
        for w in ws:
            if w[2].upper() == "CUMULATIVE":
                cum_x.append((w[0] + w[1]) / 2)
            if re.fullmatch(r"(?:19|20)\d\d", w[2]) and "JUN" in up:
                head_years.append(int(w[2]))
        if up.count("QUANTITY") >= 2:
            q = [w[1] for w in ws if w[2].upper() == "QUANTITY"]
            v = [w[1] for w in ws if w[2].upper() in ("VALUE", "RUPEES")]
            if len(q) == len(v):
                edges = list(zip(q, v))
                if years and len(years) == len(edges):
                    pair_fy = list(years)
                else:  # June / cumulative layout: [Jun, Cum] this year, [Jun, Cum] last year
                    cur = max(head_years) if head_years else None
                    pair_fy = [None] * len(edges)
                    if cur and len(edges) == 4:
                        pair_fy[1], pair_fy[3] = cur, cur - 1
                years, cum_x, head_years = [], [], []
            continue
        if not edges or any(t.upper().rstrip(":") == "PAGE" for t in texts):  # page header: "PAGE 96"
            continue
        first = ws[0]
        nums = [w for w in ws if (_NUM.match(w[2]) or w[2] in _NIL) and w[0] > edges[0][0] - 90]
        label = [w for w in ws if w not in nums]
        if re.fullmatch(r"\d{6,10}", first[2]) and first[0] < edges[0][0] - 150:
            code = first[2]
            lab = label[1:]
            unit = ""
            if len(lab) > 1 and re.fullmatch(r"[A-Z][A-Z.]{0,5}", lab[-1][2]) and lab[-1][0] > lab[0][0] + 120:
                unit, lab = lab[-1][2], lab[:-1]
            desc = re.sub(r"\s+", " ", " ".join(w[2] for w in lab)).strip()
            meta[code] = CodeMeta(code, desc, unit)
            country = ALL
        elif re.sub(r"[\s(]", "", " ".join(w[2] for w in label).upper()) in ("GRANDTOTAL", "TOTAL"):
            code, country = TOTAL, ALL
            meta.setdefault(TOTAL, CodeMeta(TOTAL, "All products", ""))
        elif code and code != TOTAL and label:
            country = clean_country(" ".join(w[2] for w in label))
            if not country or country.upper().startswith(("HS CODE", "HSCODE", "CODE ")):
                continue
        else:
            continue
        vals: dict[int, dict[str, float | None]] = {}
        for x0, x1, t in _split_glued(nums, edges):
            best = None
            for k, (qx, vx) in enumerate(edges):
                for kind, ex in (("q", qx), ("v", vx)):
                    d = abs(x1 - ex)
                    if best is None or d < best[0]:
                        best = (d, k, kind)
            if best[0] > 30:
                continue
            vals.setdefault(best[1], {})[best[2]] = None if t in _NIL else float(t.replace(",", ""))
        for k, d in vals.items():
            fy = pair_fy[k] if k < len(pair_fy) else None
            if fy is None:
                continue
            q, v = d.get("q"), d.get("v")
            if q is None and v is None:
                continue
            rows.append(TradeHsRow(flow, fy, code, country, q, v))
    return rows, meta


def _split_glued(nums: list, edges: list[tuple[float, float]]) -> list:
    """Drop words that are two adjacent figures printed with no space ('103626890017410764'): where
    one ends and the next begins can't be told, so that cell stays empty rather than guessed. No
    single figure in these volumes runs to 14 digits. Pure."""
    return [w for w in nums if len(w[2].replace(",", "")) < 14]


def parse_xlsx(content: bytes, flow: str) -> tuple[list[TradeHsRow], dict[str, CodeMeta]]:
    """D-10 workbook: HS | name/country | unit | Jun qty,pkr | Cum qty,pkr | Jun-1 | Cum-1. Pure."""
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True).active
    rows: list[TradeHsRow] = []
    meta: dict[str, CodeMeta] = {}
    fys: list[int] = []
    code = None
    for r in ws.iter_rows(values_only=True):
        if not fys:
            hs = [str(c) for c in r if c]
            cums = [re.findall(r"(\d{4})", h)[-1] for h in hs if "CUMULATIVE" in h.upper()]
            if len(cums) == 2:
                fys = [int(cums[0]), int(cums[1])]
            continue
        label = str(r[1] or "").strip()
        if r[0] is not None and re.fullmatch(r"\d{6,10}", str(r[0]).strip()):
            code = str(r[0]).strip().zfill(8)
            meta[code] = CodeMeta(code, re.sub(r"\s+", " ", label), str(r[2] or "").strip())
            country = ALL
        elif label.upper() == "GRAND TOTAL":
            code, country = TOTAL, ALL
            meta.setdefault(TOTAL, CodeMeta(TOTAL, "All products", ""))
        elif code and label:
            country = clean_country(label)
        else:
            continue
        for fy, (qi, vi) in zip(fys, ((5, 6), (9, 10))):
            q, v = r[qi] if len(r) > qi else None, r[vi] if len(r) > vi else None
            q = float(q) if isinstance(q, (int, float)) else None
            v = float(v) if isinstance(v, (int, float)) else None
            if (q or v) or country == ALL:
                rows.append(TradeHsRow(flow, fy, code, country, q, v))
    return rows, meta


def reconcile(rows: list[TradeHsRow]) -> list[tuple]:
    """Codes whose partner lines don't sum to the total line (beyond rounding). Pure."""
    tot: dict[tuple, float] = {}
    parts: dict[tuple, list] = {}
    for r in rows:
        if r.value_pkr_th is None or r.code == TOTAL:
            continue
        k = (r.flow, r.fy, r.code)
        if r.country == ALL:
            tot[k] = r.value_pkr_th
        else:
            parts.setdefault(k, []).append(r.value_pkr_th)
    # each partner line is rounded to Rs thousand on its own: allow half a unit per line
    return [(k, tot[k], round(sum(parts[k]), 1)) for k in tot
            if k in parts and abs(tot[k] - sum(parts[k])) > max(1.0 + 0.5 * len(parts[k]), 0.002 * abs(tot[k]))]


def parse_file(name: str, content: bytes) -> tuple[list[TradeHsRow], dict[str, CodeMeta]]:
    low = name.lower()
    flow = "import" if re.search(r"imp|import", low) and not re.search(r"exports?_by", low) else "export"
    if low.endswith(".xlsx"):
        return parse_xlsx(content, flow)
    if content[:4] == b"%PDF":
        return parse_lines(pdf_lines(content), flow)
    return parse_lines(text_lines(content), flow)


class PbsTradeHsJob(IngestionJob):
    name = "pbs_trade_hs"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        from datetime import date

        html = self.http_get(INDEX_URL).decode("utf-8", "replace")
        urls = sorted({u for u in re.findall(r'href="([^"]+)"', html) if _LINKED.search(u.rsplit("/", 1)[-1])})
        if backfill:
            urls = [f"https://web.archive.org/web/{ts}id_/{u}" for ts, u in ARCHIVED] + urls
        else:
            urls = [u for u in urls if "D-10" in u]
        out = []
        for u in urls:
            try:
                out.append(FetchedFile(u.rsplit("/", 1)[-1], self.http_get(u), date.today()))
            except Exception:  # noqa: BLE001 — one missing volume shouldn't sink the rest
                continue
        return out

    def parse(self, f: FetchedFile):
        rows, meta = parse_file(f.filename, f.content)
        return rows + list(meta.values())

    def validate(self, records):
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        from scripts.load_trade_hs import upsert_rows

        recs = [r for r, _ in validated]
        return upsert_rows([r for r in recs if isinstance(r, TradeHsRow)],
                           [r for r in recs if isinstance(r, CodeMeta)])
