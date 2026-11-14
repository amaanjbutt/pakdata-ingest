"""pcga_cotton_arrivals - cotton arriving at Pakistan's ginning factories, by district, every fortnight.

Source: Pakistan Cotton Ginners' Association (PCGA), "Consolidated statement of cotton arrivals in
factories of Pakistan" (collected with KCA and, until 2024, APTMA), https://www.pcga.org/arrival-reports/
— one PDF around the 3rd and the 18th of each month while the factories work (July → April/May), with
the figures as on the 1st/15th or the month-end ("AS ON: 15 Oct 2026."). The archive starts in October
2016. One report (18-Nov-2021) is a scanned image with no text layer: its figures were read from the page
images into a markdown table, `data/reference/pcga_transcribed/<file name>.md` (one cell per column, a
blank cell left empty), which goes through the same checks as a text report.

Each district row reads, left to right, in bales since the start of the season:
    arrivals | pressed | sold to TCP | sold to exporters/traders | sold to textile mills | total sold |
    unsold stock | unginned stock | [last year's arrivals | difference | INCREASE/(SHORT)/SAME | %] |
    fortnightly flow | factories working
The bracketed comparison with last season is absent from some reports and partly blank in others, and
any cell can be blank, so counts are matched to columns by position: each goes to the column whose right
edge it lines up with on the report's fullest total row (blank cells stay blank — no point is written).
Without positions (or if two counts land in one column) a row is read in order, its first eight counts
then its last two. A row's last cells can sit on a line of their own below a stacked "DATA WAS NOT
COLLECTED LAST YEAR" note; they are picked up before the next row.

Last season's figures: each district's "arrivals as on <date a year earlier>" column, and under each
total a "LAST YEAR'S TOTAL" row with every column for that date. They are stored only for a date that has
no report of its own (`fills_last_year`): before the archive's first report (the 2015-16 season and Jul-Sep
2016) and the in-season dates listed in `LAST_YEAR_FILL`. Every other date's own report is its record, so
each point still has one writer. For such a date: arrivals for every district, Balochistan and Sindh (its
districts summed), and every column for Punjab and Pakistan from their last-year totals; Sindh's other
columns are not stored, because PCGA's Sindh total includes Balochistan and only Balochistan's arrivals are
known. The district last-year arrivals must add up to the last-year totals, and the last-year totals must
satisfy the row arithmetic and Punjab + Sindh = Pakistan, or that column is left out for that date.

Layout quirks: the long "MIRPUR KHAS (THARPARKAR)" label overflows into its first number and the text
comes out interleaved ("(THARPARKA5R5),050" = "(THARPARKAR)" + "55,050"), so a token mixing letters and
digits is split back into the two. "BALUCHISTAN" is one row (the whole province) printed inside PCGA's
Sindh block, and "TOTAL OF SINDH" includes it: we store Balochistan from its row and Sindh as Sindh's
district rows summed. PCGA's districts are where the factories are, under PCGA's own area names
(Hyderabad, Nawabshah …); "Mirpur Khas (Tharparkar)" covers both districts and is stored under that name.

Every report is checked before it is used: each row must satisfy PCGA's own arithmetic (arrivals −
pressed = unginned, pressed − sold = unsold, TCP + exporters + mills = sold) and the district rows
must add up to the Punjab, Sindh and Pakistan totals, column by column. A column that fails is left out
of that report (logged); the rest of the report is kept.

Series (dims `level` — national, province, district — plus `province` and `district`), dated as on
the report's cut-off day:
    agriculture.cotton.arrivals             bales, season to date
    agriculture.cotton.pressed              bales pressed (ginned), season to date
    agriculture.cotton.sales                bales sold, season to date — dim `buyer`
                                            (all, tcp, exporters, textile_mills)
    agriculture.cotton.unsold_stock         pressed bales not yet sold
    agriculture.cotton.unginned_stock       arrivals not yet ginned (bales)
    agriculture.cotton.fortnightly_arrivals bales that arrived since the previous report
    agriculture.cotton.factories_working    ginning factories working in the fortnight
pcga.org answers the VPS: a daily run reads the newest reports; `--backfill` reads the whole archive.
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
from datetime import date

from ingestion import geo_pk
from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

LIST_URL = "https://www.pcga.org/arrival-reports/"
SOURCE_URL = LIST_URL
LATEST_N = 3  # a daily run re-reads the newest few reports (PCGA re-uploads corrected ones)
BACKFILL_PAUSE_S = 1.0

# column order of the eight leading numbers, then the two trailing ones
CORE = ("arrivals", "pressed", "sold_tcp", "sold_exporters", "sold_textile_mills", "sold",
        "unsold_stock", "unginned_stock")
TAIL = ("fortnightly_arrivals", "factories_working")
COLUMNS = CORE + TAIL
LY = "last_year_arrivals"  # the comparison column: this district's arrivals as on the same date last season

# The archive's first report is as on 1 Oct 2016: anything earlier exists only as a later report's
# "last year" figures. LAST_YEAR_FILL: in-season dates with no report of their own whose figures a
# report a year later prints as last year's (docs/backlog.md -> cotton lists the dates no source has).
FIRST_REPORT = date(2016, 10, 1)
LAST_YEAR_FILL: frozenset[date] = frozenset({
    date(2019, 10, 15),  # no report in the archive; the report as on 15 Oct 2020 prints it
    date(2023, 10, 31),  # no report in the archive; the report as on 31 Oct 2024 prints it
})
TRANSCRIBED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "data", "reference", "pcga_transcribed")

_SERIES = {
    "arrivals": ("agriculture.cotton.arrivals", {}),
    "pressed": ("agriculture.cotton.pressed", {}),
    "sold": ("agriculture.cotton.sales", {"buyer": "all"}),
    "sold_tcp": ("agriculture.cotton.sales", {"buyer": "tcp"}),
    "sold_exporters": ("agriculture.cotton.sales", {"buyer": "exporters"}),
    "sold_textile_mills": ("agriculture.cotton.sales", {"buyer": "textile_mills"}),
    "unsold_stock": ("agriculture.cotton.unsold_stock", {}),
    "unginned_stock": ("agriculture.cotton.unginned_stock", {}),
    "fortnightly_arrivals": ("agriculture.cotton.fortnightly_arrivals", {}),
    "factories_working": ("agriculture.cotton.factories_working", {}),
}

_C = dict(module="real", frequency="fortnightly", source="PCGA", source_url=SOURCE_URL, min_value=0)
# the series page charts the first headline value of each declared dimension: Pakistan's figure
_LEVELS = {"level": ["national", "province", "district"]}
_G = ("for Pakistan, Punjab, Sindh, Balochistan and every cotton district (dimension level: national, "
      "province or district, with province and district), as on the 1st/15th of each month of the season "
      "(Pakistan Cotton Ginners' Association, October 2016 →)")
CATALOG = [
    dict(id="agriculture.cotton.arrivals", name="Cotton arrivals at ginning factories", unit="bales",
         max_value=2e7,
         description=f"Seed cotton that has reached Pakistan's ginning factories since the season began, in "
                     f"bales, {_G}. The season runs from July; the count restarts each season.", **_C),
    dict(id="agriculture.cotton.pressed", name="Cotton bales pressed (ginned)", unit="bales", max_value=2e7,
         description=f"Lint cotton bales pressed by the ginning factories since the season began, {_G}.", **_C),
    dict(id="agriculture.cotton.sales", name="Cotton bales sold by ginners", unit="bales", max_value=2e7,
         dimensions={**_LEVELS, "buyer": ["all", "textile_mills", "exporters", "tcp"]},
         description=f"Bales sold since the season began by buyer (dimension buyer: all, textile_mills, "
                     f"exporters — exporters and traders —, tcp — Trading Corporation of Pakistan), {_G}.", **_C),
    dict(id="agriculture.cotton.unsold_stock", name="Unsold cotton stock at ginning factories", unit="bales",
         max_value=1e7, description=f"Pressed bales still unsold at the factories, {_G}.", **_C),
    dict(id="agriculture.cotton.unginned_stock", name="Unginned cotton stock at ginning factories",
         unit="bales", max_value=1e7,
         description=f"Seed cotton that has arrived but is not yet ginned, in bales, {_G}.", **_C),
    dict(id="agriculture.cotton.fortnightly_arrivals", name="Cotton arrivals in the fortnight", unit="bales",
         max_value=5e6, description=f"Bales of cotton that reached the factories since the previous report "
                                    f"(about two weeks), {_G}.", **_C),
    dict(id="agriculture.cotton.factories_working", name="Ginning factories working", unit="count",
         max_value=2000, description=f"Number of ginning factories working in the fortnight, {_G}.", **_C),
]
for _c in CATALOG:
    _c.setdefault("dimensions", _LEVELS)

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_ASON = re.compile(r"AS\s+ON\s*:?\s*(\d{1,2})\s*(?:st|nd|rd|th)?\.?\s*([A-Za-z]{3,9})\.?,?\s*(\d{4})", re.I)
_NUM = re.compile(r"^\(?-?[\d,]*\d(?:\.\d+)?%?\)?$")
_TOTAL = re.compile(r"^\s*TOTAL OF (PUNJAB|SINDH|PAKISTAN)\b", re.I)
_LYTOTAL = re.compile(r"^\s*LAST\s+YEAR'?S?\s+TOTAL", re.I)
# last season's date sits under "ARRIVALS AS" in the header ("ON:01.11.2020"); the report's own date is in words
_LYDATE = re.compile(r"\bON\s*:?\s*(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b", re.I)
_SERIAL = re.compile(r"^\d{1,2}$")
_LINK = re.compile(r'href="(https://www\.pcga\.org/wp-content/uploads/(\d{4})/(\d{2})/[^"]+\.pdf)"', re.I)
_NOT_REPORTS = re.compile(r"member|office-bearer|list-of", re.I)

# PCGA area labels that aren't a single district, or that geo_pk can't place
AREAS = {"mirpur khas": ("Sindh", "Mirpur Khas & Tharparkar")}

Token = tuple[str, "float | None"]  # text, right edge in points (None when unknown)


def as_on(text: str) -> date:
    m = _ASON.search(text)
    if not m:
        raise ValueError("no 'AS ON' date in the report")
    return date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))


def _number(tok: str) -> float | None:
    """'1,638,497' -> 1638497; '(36,529)' -> -36529; '11.59%' -> None (not a count). Pure."""
    if tok.endswith("%") or tok.endswith("%)"):
        return None
    neg = tok.startswith("(") or tok.startswith("-")
    v = float(tok.strip("()-").replace(",", "") or 0)
    return -v if neg else v


def line_tokens(line: dict) -> list[Token]:
    """A pdfplumber text line (`extract_text_lines(return_chars=True)`) -> its space-separated tokens,
    each with the right edge of its last character (None throughout if the characters don't line up
    with the text). Pure."""
    chars = [c for c in line["chars"] if not c["text"].isspace()]
    text = line["text"]
    known = len(chars) == sum(not ch.isspace() for ch in text)
    out: list[list] = []
    cur: list | None = None
    i = 0
    for ch in text:
        if ch.isspace():
            cur = None
            continue
        x1 = chars[i]["x1"] if known else None
        i += 1
        if cur is None:
            cur = [ch, x1]
            out.append(cur)
        else:
            cur[0] += ch
            cur[1] = x1
    return [(t, x) for t, x in out]


def split_row(tokens: list[Token]) -> tuple[str, list[tuple[float, float | None]]]:
    """[('MIRPUR', ..), ('KHAS', ..), ('(THARPARKA5R5),050', 242.1), ('52,700', 301.0), ...] ->
    ('MIRPUR KHAS (THARPARKAR)', [(55050, 242.1), (52700, 301.0), ...]): the label, then every count on
    the line with its right edge (percentages and words dropped). Pure."""
    name: list[str] = []
    nums: list[tuple[float, float | None]] = []
    for tok, x in tokens:
        if _NUM.match(tok):
            v = _number(tok)
            if v is not None:
                nums.append((v, x))
        elif not nums and re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok):
            # the label ran into the first number: letters back to the label, digits to the number
            name.append(re.sub(r"[\d,]", "", tok))
            nums.append((float(re.sub(r"[^\d]", "", tok)), x))
        elif not nums:
            name.append(tok)
    return " ".join(name).strip(" :"), nums


def _columns(nums: list[tuple[float, float | None]], template: list[float] | None) -> dict | None:
    """A row's counts by column. With the report's column template (the right edges of a total row's
    counts) each count goes to the column it lines up with, so a blank cell stays blank (None). Without
    one — or if two counts land in one column — they are read in order: the first eight, then the last
    two (None when the row stops after eight). None when there aren't eight counts."""
    if template and len(template) >= len(COLUMNS) and nums and all(x is not None for _, x in nums):
        slots = dict(enumerate(CORE)) | {len(template) - 2: TAIL[0], len(template) - 1: TAIL[1]}
        if len(template) >= len(COLUMNS) + 2:  # last year's arrivals, then the difference
            slots[len(CORE)] = LY
        out: dict = dict.fromkeys(COLUMNS + (LY,))
        for v, x in nums:
            col = slots.get(min(range(len(template)), key=lambda k: abs(template[k] - x)))
            if col is None:
                continue  # the difference from last season
            if out[col] is not None:
                break
            out[col] = v
        else:
            if out["arrivals"] is not None:
                return out
    vals = [v for v, _ in nums]
    if len(vals) < len(CORE):
        return None
    tail = vals[-len(TAIL):] if len(vals) >= len(COLUMNS) else [None] * len(TAIL)
    # in order, last year's arrivals is known only when the whole comparison is there
    ly = vals[len(CORE)] if len(vals) == len(COLUMNS) + 2 else None
    return dict(zip(COLUMNS, vals[:len(CORE)] + tail)) | {LY: ly}


def _text(line: list[Token]) -> str:
    return " ".join(t for t, _ in line)


def _is_row(line: list[Token]) -> bool:
    """A district row starts with its serial number and then its label ('17 BHAKKAR …'); a line of
    stray cells ('0 0') doesn't."""
    return len(line) >= 2 and bool(_SERIAL.match(line[0][0])) and bool(re.match(r"\(?[A-Za-z]", line[1][0]))


def place(label: str) -> tuple[str, str | None] | None:
    """PCGA area label -> (province, district or None for a whole province). Pure."""
    key = re.sub(r"[^a-z]+", " ", label.lower()).strip()
    if key.startswith("baluchistan") or key.startswith("balochistan"):
        return "Balochistan", None
    for prefix, hit in AREAS.items():
        if key.startswith(prefix):
            return hit
    hit = geo_pk.canon_district(label)
    return (hit[0], hit[1]) if hit else None


def parse_lines(lines: list[list[Token]]) -> tuple[date, list[dict], dict[str, dict]]:
    """Report lines (tokens with right edges) -> (as-on date, district rows, PCGA totals keyed
    'punjab' / 'sindh' / 'pakistan'). Each row: {zone, province, district, label, <column>: value}. Pure."""
    when = as_on("\n".join(_text(ln) for ln in lines))
    totals_raw = {i: split_row(ln)[1] for i, ln in enumerate(lines) if _TOTAL.match(_text(ln))}
    template = _template(totals_raw)
    rows: list[dict] = []
    totals: dict[str, dict] = {}
    zone = "punjab"
    for i, line in enumerate(lines):
        if i in totals_raw:
            which = _TOTAL.match(_text(line)).group(1).lower()
            cols = _columns(totals_raw[i], template)
            if cols:
                totals[which] = cols
            if which == "punjab":
                zone = "sindh"
            continue
        if not _is_row(line):
            continue
        label, nums = split_row(line[1:])
        if not re.search(r"[A-Za-z]{3}", label):
            continue
        cols = _columns(nums, template)
        # A "DATA WAS NOT COLLECTED LAST YEAR" note stacked down the comparison column can push a
        # row's last cells onto a line of their own below it: pick them up before the next row.
        j = i + 1
        while (cols is None or cols[TAIL[0]] is None or cols[TAIL[1]] is None) and j < len(lines) \
                and not _is_row(lines[j]) and "TOTAL" not in _text(lines[j]):
            extra = lines[j]
            if extra and all(_NUM.match(t) for t, _ in extra):
                nums = nums + [(v, x) for t, x in extra if (v := _number(t)) is not None]
                cols = _columns(nums, template)
            j += 1
        if cols is None:
            continue
        where = place(label)
        if where is None:
            raise ValueError(f"unknown PCGA area {label!r}")
        rows.append({"zone": zone, "province": where[0], "district": where[1], "label": label, **cols})
    if not rows or "pakistan" not in totals:
        raise ValueError("no district rows / Pakistan total found")
    return when, rows, totals


def _template(totals_raw: dict[int, list]) -> list[float] | None:
    """The column template: the right edges of the counts on the total row with the most counts
    (totals print every column). None without positions."""
    template = max(([x for _, x in nums] for nums in totals_raw.values()), key=len, default=None)
    return None if template and any(x is None for x in template) else template


def parse_last_year(lines: list[list[Token]]) -> tuple[date | None, dict[str, dict]]:
    """The date of last season's comparison ('ARRIVALS AS ON:01.10.2015') and the "LAST YEAR'S TOTAL"
    rows printed under the Punjab / Sindh / Pakistan totals, by column (a row reading "DATA WAS NOT
    COLLECTED LAST YEAR" is left out). Pure."""
    m = _LYDATE.search("\n".join(_text(ln) for ln in lines))
    when = date(int(m.group(3)) % 100 + 2000, int(m.group(2)), int(m.group(1))) if m else None
    totals_raw = {i: split_row(ln)[1] for i, ln in enumerate(lines) if _TOTAL.match(_text(ln))}
    template = _template(totals_raw)
    out: dict[str, dict] = {}
    zone = None
    for i, line in enumerate(lines):
        if i in totals_raw:
            zone = _TOTAL.match(_text(line)).group(1).lower()
        elif zone and _LYTOTAL.match(_text(line)):
            cols = _columns(split_row(line)[1], template)
            if cols and cols["arrivals"] is not None:
                out[zone] = cols
            zone = None
    return when, out


def fills_last_year(when: date | None) -> bool:
    """Whether a report's "last year" figures are the record for that date (no report of its own). Pure."""
    return when is not None and (when < FIRST_REPORT or when in LAST_YEAR_FILL)


def last_year_failed(rows: list[dict], totals: dict[str, dict], ly_totals: dict[str, dict]) -> dict[str, str]:
    """Columns of last season's figures that don't add up, with the reason: the district last-year
    arrivals vs the last-year totals (or, without those rows, the comparison printed on each total row),
    and the last-year totals' own arithmetic and Punjab + Sindh = Pakistan. Pure."""
    bad: dict[str, str] = {}
    sums = {z: sum(r[LY] or 0 for r in rows if r["zone"] == z) for z in ("punjab", "sindh")}
    sums["pakistan"] = sums["punjab"] + sums["sindh"]
    for z in ("punjab", "sindh", "pakistan"):
        want = (ly_totals.get(z) or {}).get("arrivals")
        if want is None:
            want = (totals.get(z) or {}).get(LY)
        if want is None:
            if z == "pakistan":
                bad.setdefault("arrivals", "no last-year Pakistan total")
            continue
        if abs(sums[z] - want) > 1:
            bad.setdefault("arrivals", f"last-year {z} districts {sums[z]:,.0f} ≠ total {want:,.0f}")
    for z, v in ly_totals.items():
        n = {c: v[c] or 0 for c in CORE}
        for col, want, got in (("unginned_stock", n["arrivals"] - n["pressed"], n["unginned_stock"]),
                               ("unsold_stock", n["pressed"] - n["sold"], n["unsold_stock"]),
                               ("sold", n["sold_tcp"] + n["sold_exporters"] + n["sold_textile_mills"], n["sold"])):
            if abs(want - got) > 1:
                bad.setdefault(col, f"last-year {z}: {col} {got:,.0f} ≠ {want:,.0f}")
    if all(z in ly_totals for z in ("punjab", "sindh", "pakistan")):
        for col in COLUMNS:
            p, s, k = (ly_totals[z][col] for z in ("punjab", "sindh", "pakistan"))
            if None not in (p, s, k) and abs(p + s - k) > 1:
                bad.setdefault(col, f"last-year Punjab + Sindh {p + s:,.0f} ≠ Pakistan {k:,.0f}")
    return bad


def last_year_records(when: date, rows: list[dict], totals: dict[str, dict], ly_totals: dict[str, dict],
                      skip: set[str]) -> list[Record]:
    """Last season's figures as on `when`: arrivals for every district, Balochistan and Sindh (its
    districts summed); every column for Punjab and Pakistan from their last-year totals (arrivals from
    the total row's comparison when no last-year total is printed). Pure."""
    out: list[Record] = []
    arr = _SERIES["arrivals"][0]
    if "arrivals" not in skip:
        for r in rows:
            if r[LY] is None:
                continue
            dims = ({"level": "district", "province": r["province"], "district": r["district"]} if r["district"]
                    else {"level": "province", "province": r["province"]})
            out.append(Record(arr, when, float(r[LY]), dims))
        sindh = [r[LY] for r in rows if r["zone"] == "sindh" and r["province"] == "Sindh"]
        if any(v is not None for v in sindh):
            out.append(Record(arr, when, float(sum(v or 0 for v in sindh)), {"level": "province", "province": "Sindh"}))
    for z, dims in (("punjab", {"level": "province", "province": "Punjab"}), ("pakistan", {"level": "national"})):
        vals = dict(ly_totals.get(z) or {})
        if vals.get("arrivals") is None and (totals.get(z) or {}).get(LY) is not None:
            vals = {"arrivals": totals[z][LY]}
        for col in COLUMNS:
            if col in skip or vals.get(col) is None:
                continue
            sid, extra = _SERIES[col]
            out.append(Record(sid, when, float(vals[col]), {**dims, **extra}))
    return out


def transcribed(filename: str) -> str | None:
    """The markdown read from a scanned report's page images, if there is one."""
    path = os.path.join(TRANSCRIBED_DIR, filename.rsplit(".", 1)[0] + ".md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def transcribed_lines(md: str) -> list[list[Token]]:
    """A transcription -> report lines. Text outside the table carries the dates ('AS ON: 15TH NOVEMBER
    2021', 'ARRIVALS AS ON: 15.11.2020'). Each table row is `| S.No | name | <14 cells> |` in the report's
    column order (arrivals … unginned stock, last year's arrivals, difference, INCREASE/SHORT, %, fortnightly
    flow, factories); a cell's index stands in for its position, so an empty cell stays blank instead of
    shifting the row. The header and separator rows are skipped. Pure."""
    out: list[list[Token]] = []
    for raw in md.replace("\u2019", "'").replace("\u2018", "'").splitlines():
        ln = raw.strip()
        if not ln.startswith("|"):
            out.append([(t, None) for t in ln.split()])
            continue
        cells = [c.strip().replace(" ", "") if i > 1 else c.strip()
                 for i, c in enumerate(ln.strip("|").split("|"))]
        if len(cells) < 3 or not any(_NUM.match(c) for c in cells[2:]):
            continue  # the header row or the |---| separator (no counts)
        line: list[Token] = [(t, None) for t in (cells[0] + " " + cells[1]).split()]
        line += [(c, float(i)) for i, c in enumerate(cells[2:]) if c]
        out.append(line)
    return out


def parse_text(text: str) -> tuple[date, list[dict], dict[str, dict]]:
    """`parse_lines` for plain text (no positions: counts are read in order). Pure."""
    return parse_lines([[(t, None) for t in ln.split()] for ln in text.splitlines()])


def failed_columns(rows: list[dict], totals: dict[str, dict]) -> dict[str, str]:
    """Columns whose figures don't add up, with the reason — row identities and district sums vs
    the Punjab / Sindh / Pakistan totals. Pure."""
    bad: dict[str, str] = {}
    for r in rows:
        v = {c: r[c] or 0 for c in CORE}  # a blank cell (None) adds nothing
        checks = (("unginned_stock", v["arrivals"] - v["pressed"], v["unginned_stock"]),
                  ("unsold_stock", v["pressed"] - v["sold"], v["unsold_stock"]),
                  ("sold", v["sold_tcp"] + v["sold_exporters"] + v["sold_textile_mills"], v["sold"]))
        for col, want, got in checks:
            if abs(want - got) > 1:
                bad.setdefault(col, f"{r['label']}: {col} {got:,.0f} ≠ {want:,.0f}")
    for col in COLUMNS:
        # the district sums (blank cells adding nothing) must reach the printed totals
        sums = {z: sum(r[col] or 0 for r in rows if r["zone"] == z) for z in ("punjab", "sindh")}
        for z in ("punjab", "sindh", "pakistan"):
            if z in totals and totals[z][col] is None:
                bad.setdefault(col, f"no {z} total")
        if col in bad:
            continue
        for z in ("punjab", "sindh"):
            if z in totals and abs(sums[z] - totals[z][col]) > 1:
                bad.setdefault(col, f"{z} districts {sums[z]:,.0f} ≠ total {totals[z][col]:,.0f}")
        if abs(sums["punjab"] + sums["sindh"] - totals["pakistan"][col]) > 1:
            bad.setdefault(col, f"districts {sums['punjab'] + sums['sindh']:,.0f} ≠ Pakistan "
                                f"{totals['pakistan'][col]:,.0f}")
    return bad


def to_records(when: date, rows: list[dict], totals: dict[str, dict], skip: set[str]) -> list[Record]:
    """District, province and Pakistan records for every column not in `skip`. Pure."""
    out: list[Record] = []
    places: list[tuple[dict, dict]] = []
    for r in rows:
        dims = ({"level": "district", "province": r["province"], "district": r["district"]} if r["district"]
                else {"level": "province", "province": r["province"]})
        places.append((dims, r))
    sindh = {c: sum(r[c] or 0 for r in rows if r["zone"] == "sindh" and r["province"] == "Sindh")
             for c in COLUMNS}
    places += [({"level": "province", "province": "Punjab"}, totals["punjab"]),
               ({"level": "province", "province": "Sindh"}, sindh), ({"level": "national"}, totals["pakistan"])]
    for dims, vals in places:
        for col in COLUMNS:
            if col in skip or vals[col] is None:
                continue
            sid, extra = _SERIES[col]
            out.append(Record(sid, when, float(vals[col]), {**dims, **extra}))
    return out


def pdf_lines(content: bytes) -> list[list[Token]]:
    """Every text line of a report PDF as tokens with right edges."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return [line_tokens(ln) for p in pdf.pages for ln in p.extract_text_lines(return_chars=True)]


def published(url: str, folder: tuple[str, str]) -> date:
    """Publication date from the file name ('18-October-2026.pdf', '10-01-February-2019.pdf',
    '3-JANUARY-2022.pdf'), else the upload folder's month. Pure."""
    m = re.search(r"(\d{1,2})-([A-Za-z]{3,9})-(\d{4})", url.rsplit("/", 1)[-1])
    if m and m.group(2)[:3].lower() in MONTHS:
        try:
            return date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
        except ValueError:
            pass
    return date(int(folder[0]), int(folder[1]), 1)


def report_links(page: str) -> list[str]:
    """Report PDF urls on the archive page, oldest first by publication date (uploads are batched —
    September's reports sit in the October folder — and one old link repeats near the top of the
    page, so neither folder nor page order will do). Pure."""
    seen: dict[str, date] = {}
    for url, y, mo in _LINK.findall(page):
        if not _NOT_REPORTS.search(url):
            seen.setdefault(url, published(url, (y, mo)))
    return sorted(seen, key=lambda u: (seen[u], u))


class PcgaCottonArrivalsJob(IngestionJob):
    name = "pcga_cotton_arrivals"
    source = "PCGA"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        links = report_links(self.http_get(LIST_URL).decode("utf-8", "replace"))
        if not links:
            raise ValueError("no arrival reports linked on the PCGA archive page")
        if not backfill:
            links = links[-LATEST_N:]
        out: list[FetchedFile] = []
        errors: list[Exception] = []
        for i, url in enumerate(links):
            if backfill and i:
                time.sleep(BACKFILL_PAUSE_S)  # a small association's site: ~150 files, spaced out
            try:
                content = self.http_get(url)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                continue
            if content[:4] == b"%PDF":
                out.append(FetchedFile(filename=url.rsplit("/", 1)[-1], content=content, when=date.today()))
        if errors and not out:
            raise errors[0]
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        lines = pdf_lines(f.content)
        if not any(lines):
            text = transcribed(f.filename)
            if text is None:
                raise ValueError(f"{f.filename}: no text layer (scanned report) and no transcription")
            lines = transcribed_lines(text)
        return report_records(lines, f.filename)


def report_records(lines: list[list[Token]], filename: str = "") -> list[Record]:
    """One report's records: its own date, plus last season's figures when that date has no report of
    its own. A column that doesn't add up is left out (logged)."""
    when, rows, totals = parse_lines(lines)
    bad = failed_columns(rows, totals)
    for col, why in bad.items():
        log.warning("pcga_cotton_arrivals: %s %s left out — %s", filename, col, why)
    out = to_records(when, rows, totals, set(bad))
    ly_when, ly_totals = parse_last_year(lines)
    if fills_last_year(ly_when) and ly_when < when:
        ly_bad = last_year_failed(rows, totals, ly_totals)
        for col, why in ly_bad.items():
            log.warning("pcga_cotton_arrivals: %s last year (%s) %s left out — %s", filename, ly_when, col, why)
        out += last_year_records(ly_when, rows, totals, ly_totals, set(ly_bad))
    return out
