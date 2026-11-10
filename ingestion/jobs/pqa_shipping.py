"""pqa_shipping - Port Qasim's daily shipping and cargo statement: every ship at a berth, at the outer
anchorage and expected, as at 08:00 each day since 31 May 2019.

Source: Port Qasim Authority (PQA), Marine Operation Department, "Daily Shipping and Cargo Statement as
at 0800 hrs" — one signed PDF a day in the album
https://www.pqa.gov.pk/en/documents/album/daily-shipping-and-cargo-handling-reports (`?page=N`, ten a
page, newest first; ~2,450 statements from 31-May-2019, with gaps). File names changed many times
(`dailyShippingCargoStat_DD-MON-YYYY-<guid>.PDF`, `dailyShippingCargo_dd-mon-YYYY-…`,
`dailyShippingCargo Handling Report on DD-Month-YY-…`, typos) so the date is read from the header
("… AS AT 0800 HRS … ON 30 OCT 2026"). PQA's HTML pages (/pqa/portoperation/ShipsAtOuterAnchorage,
BerthWiseCargoHandling) are not used: on 2026-10-31 they listed different ships from the signed PDF.

The statement (pdfplumber tables; cells are read character by character with their positions):
  * Berth-wise table, 14 columns, page 1 and the top of page 2: berth, vessel, pilot, LOA, PQA
    registration no., berthing date and time, agent, commodity (with a unit line — "M.Ton", "C.Met" for
    LNG; containers carry none), manifest quantity, cargo handled in the last 24 hours, previously,
    balance, cast-off. The direction is not printed: each commodity is a sub-row about 20 pt high
    whose import (unloaded) figures sit on its first line (−4 … +2 pt from the commodity name) and
    export (loaded) figures on its second (+7 … +10 pt) — container ships fill both. A name can wrap
    ("SOYABEAN / SEED", +8 pt); a ship can carry several commodities (a sub-row each); two ships on one
    berth share the cells (split on their registration numbers, whose line the import figures can sit
    up to 4 pt above). An empty berth cell continues the previous berth (page breaks). "NIL" = 0,
    "NO WORK" = 0 (noted), "--" = not applicable. Each figure line is checked: manifest = last 24 h +
    previous + balance; a line that fails stays in the vessel table (`checks_ok` false) but is left out
    of the derived totals.
  * Vessels at outer anchorage, 13 columns matched by header (2019–2021: L.O.A, Ship Name, Draf Aft,
    Draf Far …; later: Terminal, Ship Name, LOA, Draf Aft …): registration time and arrival date, agent,
    flag (Nationality), loading port (a port or a country; "PQA" for export cargo), commodity, weight,
    Imp/Exp — explicit per commodity line.
  * Expected ship arrivals (8 columns; often "No Ships are expected"), and from 2022 the previous day's
    sailings (one text cell per row: "1 38074 M.T- SHANDONG REDWOOD EETL 30-10-2026 645").
  * "No. of vessels berthed": a running count per terminal since it opened, with PQA's total
    (checked: the terminals must add up to it).
Tide tables, pilot rosters, planned movements and remarks are not stored.

Stored:
  * `port_vessel_calls` (see `ingestion/port_calls.py`): every ship × commodity × direction of each
    statement (sections berth / anchorage / expected / sailed), raw text beside readable names from
    `ingestion/ports_pk.py`. A ship is one row per statement it appears in, so restated manifests and
    balances are visible day to day.
  * Daily series (module external, dated by the statement, as at 08:00 that day; level total |
    terminal | commodity | loading_country, direction all | import | export, one unit per series):
        ports.qasim.cargo_handled        t handled in the 24 hours to 08:00 (bulk, break-bulk, liquids)
        ports.qasim.containers_handled   TEUs handled in the 24 hours (PQA prints no unit: the daily figures
                                         sum to its annual TEU table within 2%)
        ports.qasim.lng_handled          m³ of LNG discharged in the 24 hours
        ports.qasim.ships_at_berth       ships alongside at 08:00
        ports.qasim.ships_at_anchorage   ships waiting at the outer anchorage
        ports.qasim.cargo_at_anchorage   t aboard the ships waiting (tonnage lines only)
        ports.qasim.expected_arrivals    ships PQA expects next
        ports.qasim.berthings_to_date    vessels berthed since each terminal opened (PQA's running count)
pqa.gov.pk answers the VPS: a daily run reads the newest statements; `--backfill` walks the album.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ingestion import ports_pk
from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.port_calls import PortCallsMixin, VesselRow, number_seqs

log = logging.getLogger("pakdata.ingest")

ALBUM_URL = "https://www.pqa.gov.pk/en/documents/album/daily-shipping-and-cargo-handling-reports"
SOURCE_URL = ALBUM_URL
LATEST_N = 5          # a daily run re-reads the newest few statements (PQA sometimes re-uploads a day)
BACKFILL_PAUSE_S = 1.0
PORT = "PQA"

_LINK = re.compile(r'href="(https?://www\.pqa\.gov\.pk/Content/Media/+daily-shipping-and-cargo-handling-reports/'
                   r'[^"]+\.pdf)"', re.I)
_PAGE = re.compile(r"[?&]page=(\d+)")
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_ON = re.compile(r"\bON\s*(\d{1,2})(?:ST|ND|RD|TH)?[\s.-]*([A-Z]{3,9})[\s.,-]*(\d{4}|\d{2})\b", re.I)
_REG = re.compile(r"^\d{3,6}(?:-\d{1,2})?$")
_PREFIX = re.compile(r"^(?:M\s*[./]?\s*[VTS]\b\.?|MV\b|MT\b|TUG\b|LPG/C\b)\s*-?\s*", re.I)
_SUFFIX = re.compile(r"\s+M\.?\s*[VT]\.?$", re.I)
_NUM = re.compile(r"^-?[\d,]*\d(?:\.\d+)?$")
_DM = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")
_HM = re.compile(r"^(\d{1,2})[:.]?(\d{2})$")

BERTH_COLS = ("berth", "vessel", "pilot", "loa", "reg", "berth_date", "berth_time", "agent", "commodity",
              "manifest", "last_24h", "previous", "balance", "cast_off")
FIGURES = ("manifest", "last_24h", "previous", "balance")
ANCHORAGE_HEADERS = (("vessel", ("SHIP NAME",)), ("berth", ("TERMINAL",)), ("loa", ("L.O.A", "LOA")),
                     ("draft", ("DRAF AFT", "DRAFT AFT")), ("reg", ("REGNO", "REG NO", "REG.NO")),
                     ("regd", ("REGD",)), ("arrived", ("ARRIVED",)), ("agent", ("AGENT",)),
                     ("flag", ("NATIONALITY",)), ("port", ("LOADING PORT", "LAST PORT")),
                     ("commodity", ("COMMODITY",)), ("weight", ("WEIGHT", "TONNE")),
                     ("direction", ("IMP/EXP", "IMPORT/EXPORT", "IMP / EXP")), ("due", ("DUE DATE",)))
SUB_ROW = 14.0     # a commodity's two lines span < 14 pt; the next commodity starts ~20 pt below
IMPORT_LINE = 4.5  # figures less than this below the commodity name are on its import line
ABOVE = 6.0        # figures can sit this far above the line they belong to

Line = tuple[float, str]  # (top, text) — one text line of a table cell


# ---------------------------------------------------------------------------------------------------------
# geometry helpers (pure)

def cell_lines(chars: list[dict], bbox: tuple[float, float, float, float], tol: float = 2.5) -> list[Line]:
    """The text lines of the characters whose centres fall in `bbox`, top to bottom, each with the
    top of its highest character. A gap wider than a fifth of the font size is a space. Pure."""
    x0, top, x1, bottom = bbox
    cs = [c for c in chars if x0 <= (c["x0"] + c["x1"]) / 2 < x1 and top <= (c["top"] + c["bottom"]) / 2 < bottom]
    rows: list[list[dict]] = []
    for c in sorted(cs, key=lambda c: c["top"]):
        if rows and abs(c["top"] - rows[-1][0]["top"]) <= tol:
            rows[-1].append(c)
        else:
            rows.append([c])
    out: list[Line] = []
    for r in rows:
        r.sort(key=lambda c: c["x0"])
        text, prev = "", None
        for c in r:
            if prev is not None and c["x0"] - prev["x1"] > 0.2 * float(c.get("size") or 8):
                text += " "
            text += c["text"]
            prev = c
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out.append((min(c["top"] for c in r), text))
    return out


def between(lines: list[Line], lo: float, hi: float) -> list[Line]:
    return [(y, t) for y, t in lines if lo <= y < hi]


def joined(lines: list[Line]) -> str:
    return " ".join(t for _, t in lines).strip()


def figure(text: str) -> tuple[float | None, str | None]:
    """'40,105' -> (40105, None); 'NIL' -> (0, None); 'NO WORK' -> (0, 'no work'); '--' -> (None, None).
    Pure."""
    t = text.strip().upper()
    if not t or set(t) <= set("-–. "):
        return None, None
    if t in ("NIL", "NILL", "0"):
        return 0.0, None
    if "NO WORK" in t or t == "NOWORK":
        return 0.0, "no work"
    t = t.replace(" ", "")
    if _NUM.match(t):
        return float(t.replace(",", "")), None
    return None, t.lower()


def vessel_name(text: str) -> tuple[str, str | None]:
    """'M.V- MAGIC CELESTE' -> ('MAGIC CELESTE', 'M.V'); 'EVIRIDIKI M.T' -> ('EVIRIDIKI', 'M.T'). Pure."""
    t = re.sub(r"\s+", " ", text).strip()
    m = _PREFIX.match(t)
    if m and len(t) > m.end():
        return t[m.end():].strip(" -"), re.sub(r"[\s-]", "", m.group(0)).upper().rstrip(".")
    m = _SUFFIX.search(t)
    if m:
        return t[:m.start()].strip(" -"), re.sub(r"\s", "", m.group(0)).upper().rstrip(".")
    return t.strip(" -"), None


def statement_date(text: str) -> date:
    """The 'ON 31 OCT 2026' of the header. Pure."""
    for m in _ON.finditer(text):
        mon = MONTHS.get(m.group(2)[:3].lower())
        if not mon:
            continue
        y = int(m.group(3))
        y = y + 2000 if y < 100 else y
        try:
            return date(y, mon, int(m.group(1)))
        except ValueError:
            continue
    raise ValueError("no 'ON <date>' in the statement header")


FIRST_DAY = date(2019, 5, 31)


def checked_date(header: date) -> date:
    """The statement's date is its header's. File names are no check: across the archive they are the
    stale side ('mar19-20-SEP-2022' uploaded in September 2023, '05-Sep-19' reused in October) — PQA's
    running count of vessels berthed agrees with the header in each case. One upload labelled 30-Jul-2019
    really is PQA's statement for 26 Sep 2002, reprinted by its current system (registration numbers in
    the 6,500s, 6,545 vessels berthed to date): it is kept under its own date. Only a future date, or one
    before the port opened, is refused. Pure."""
    if not date(1980, 1, 1) <= header <= date.today() + timedelta(days=1):
        raise ValueError(f"statement dated {header}, impossible")
    return header


def day_month(text: str, ref: date) -> date | None:
    """'29/10' (berthing date, no year) or '19/10/26' -> a date no later than a few days after the
    statement. Pure."""
    m = _DM.match(text.strip())
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    y = int(m.group(3)) if m.group(3) else ref.year
    y = y + 2000 if y < 100 else y
    try:
        out = date(y, mo, d)
    except ValueError:
        return None
    if not m.group(3) and out > ref + timedelta(days=7):
        out = date(y - 1, mo, d)
    return out


def hour_minute(text: str) -> tuple[int, int] | None:
    m = _HM.match(text.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)), int(m.group(2))


def at(day: date | None, hm: tuple[int, int] | None) -> datetime | None:
    if day is None:
        return None
    return datetime(day.year, day.month, day.day, *(hm or (0, 0)))


def to_float(text: str) -> float | None:
    t = text.strip().replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------
# tables

@dataclass
class Grid:
    """A table found by pdfplumber: its text, column x-bounds and row y-bounds, and the page's chars."""

    text: list[list[str]]
    xs: list[tuple[float, float]]
    ys: list[tuple[float, float]]
    chars: list[dict]
    page: int
    _band: dict = field(default_factory=dict, repr=False)

    def cell(self, row: int, col: int) -> list[Line]:
        (x0, x1), (y0, y1) = self.xs[col], self.ys[row]
        if row not in self._band:  # the row's characters, once (a page has thousands)
            self._band[row] = [c for c in self.chars if y0 <= (c["top"] + c["bottom"]) / 2 < y1]
        return cell_lines(self._band[row], (x0, y0, x1, y1))

    def row_text(self, row: int) -> str:
        return " ".join((c or "").replace("\n", " ") for c in self.text[row]).upper()


def grids(pdf) -> list[Grid]:
    """Every table on every page as a Grid. Column bounds come from the table's cells (the widest
    agreement across rows), so a merged cell can't shift a column."""
    out: list[Grid] = []
    for pno, page in enumerate(pdf.pages):
        chars = page.chars
        for t in page.find_tables():
            text = t.extract()
            if not text or not text[0]:
                continue
            ncol = len(text[0])
            bounds: list[list[tuple[float, float]]] = [[] for _ in range(ncol)]
            ys: list[tuple[float, float]] = []
            for row in t.rows:
                ys.append((row.bbox[1], row.bbox[3]))
                for i, c in enumerate(row.cells[:ncol]):
                    if c is not None:
                        bounds[i].append((c[0], c[2]))
            if any(not b for b in bounds):
                continue
            xs = [max(set(b), key=b.count) for b in bounds]
            out.append(Grid(text=[[c or "" for c in r] for r in text], xs=xs, ys=ys, chars=chars, page=pno))
    return out


def kind(g: Grid, prev: tuple[str, int] | None) -> str | None:
    """berth | anchorage | expected | sailed | berthed, from the header text; a table without one
    continues the previous table (same column count) across a page break."""
    head = " ".join(g.row_text(i) for i in range(min(2, len(g.text))))
    n = len(g.text[0])
    if "NAME OF VESSEL" in head or "MANIFEST" in head or "(IMPORT)" in head or "UNLOADED" in head:
        return "berth"
    if "SHIP NAME" in head and ("DRAF" in head or "NATIONALITY" in head or "REGNO" in head):
        return "anchorage"
    if "LAST PORT" in head or "DUE DATE" in head:
        return "expected"
    if "SALING" in head or "SAILING" in head:
        return "sailed"
    if n == 2:
        return "berthed"
    if prev and prev[0] in ("berth", "anchorage") and prev[1] == n:
        return prev[0]
    return {14: "berth", 13: "anchorage"}.get(n)


# the anchorage table's two column orders, for header cells pdfplumber reads blank
ANCHORAGE_LAYOUTS = {
    "loa_first": ("loa", "vessel", "draft", None, "reg", "regd", "arrived", "agent", "flag", "port", "commodity",
                  "weight", "direction"),   # 2019 – 2021: L.O.A, Ship Name, Draf Aft, Draf Far …
    "terminal_first": ("berth", "vessel", "loa", "draft", "reg", "regd", "arrived", "agent", "flag", "port",
                       "commodity", "weight", "direction"),
}


def header_map(g: Grid, row: int, spec=ANCHORAGE_HEADERS) -> dict[str, int]:
    """Column index of each field, matched on the header row's text."""
    out: dict[str, int] = {}
    for i, cell in enumerate(g.text[row]):
        h = re.sub(r"\s+", " ", cell.replace("\n", " ")).upper()
        for name, keys in spec:
            if name not in out and any(k in h for k in keys):
                out[name] = i
                break
    if spec is ANCHORAGE_HEADERS and len(g.text[row]) == 13:
        second = g.text[row][2].upper()
        layout = ANCHORAGE_LAYOUTS["loa_first" if "DRAF" in second or "L.O.A" in g.text[row][0].upper()
                                   else "terminal_first"]
        for i, name in enumerate(layout):
            if name and name not in out and not g.text[row][i].strip():
                out[name] = i
    return out


# ---------------------------------------------------------------------------------------------------------
# sections

@dataclass
class SubRow:
    top: float
    name: list[str] = field(default_factory=list)
    unit: str | None = None
    last: float = 0.0

    @property
    def text(self) -> str:
        return " ".join(self.name).strip()


def sub_rows(lines: list[Line]) -> list[SubRow]:
    """A ship's commodity cell -> its commodities, each with the top of its first line and its unit:
    a name line under 14 pt below the start of the current commodity (and before its unit) continues
    it ('SOYABEAN' / 'SEED'); 'PALM OIL/M.Ton' carries its unit inline. Pure."""
    out: list[SubRow] = []
    for y, t in lines:
        unit = ports_pk.unit_of(t)
        if unit:
            if out and out[-1].unit is None:
                out[-1].unit, out[-1].last = t, y
            continue
        name, _, tail = t.rpartition("/")
        if name and ports_pk.unit_of(tail):
            t, unit = name.strip(), tail.strip()
        if out and out[-1].unit is None and y - out[-1].top < SUB_ROW and y - out[-1].last < 11.5:
            out[-1].name.append(t)
            out[-1].last = y
            if unit:
                out[-1].unit = unit
            continue
        out.append(SubRow(top=y, name=[t], unit=unit or None, last=y))
    return out


def figure_lines(cols: dict[str, list[Line]], tol: float = 3.0) -> list[tuple[float, dict[str, str]]]:
    """The four figure columns' lines clustered into rows: [(top, {'manifest': '15500', …})]. Pure."""
    pts = sorted(((y, c, t) for c, ls in cols.items() for y, t in ls), key=lambda p: p[0])
    out: list[tuple[float, dict[str, str]]] = []
    for y, c, t in pts:
        if out and y - out[-1][0] <= tol and c not in out[-1][1]:
            out[-1][1][c] = t
        else:
            out.append((y, {c: t}))
    return out


def assign(subs: list[SubRow], lines: list[tuple[float, dict]], anchor: float
           ) -> list[tuple[SubRow | None, str, dict]]:
    """Each figure line -> (commodity sub-row, 'import' | 'export', figures): the sub-row it falls in,
    import on the sub-row's first line, export on its second. A ship without a commodity name reads
    its lines against its registration number's line. Pure."""
    out = []
    starts = [s.top for s in subs] or [anchor]
    for y, figs in lines:
        i = max([k for k, s in enumerate(starts) if y >= s - ABOVE] or [0])
        off = y - starts[i]
        if off >= SUB_ROW + ABOVE and i == len(starts) - 1:
            # past the last named commodity: an unnamed sub-row (PQA left the name blank)
            extra = off - round(off / 20.5) * 20.5
            out.append((None, "import" if extra < IMPORT_LINE else "export", figs))
            continue
        out.append((subs[i] if subs else None, "import" if off < IMPORT_LINE else "export", figs))
    return out


def ship_blocks(g: Grid, r: int, cols: dict[str, int]) -> list[tuple[float, float]]:
    """The vertical span of each ship in a table row: split on the registration numbers (or, without
    one, the vessel-name lines)."""
    y0, y1 = g.ys[r]
    anchors = [y for y, t in g.cell(r, cols["reg"]) if _REG.match(t.replace(" ", ""))] if "reg" in cols else []
    if not anchors:
        names = [y for y, t in g.cell(r, cols["vessel"]) if t.strip()]
        anchors = names[:1]
    if not anchors:
        return []
    spans = []
    for i, a in enumerate(anchors):
        lo = y0 if i == 0 else a - ABOVE
        hi = anchors[i + 1] - ABOVE if i + 1 < len(anchors) else y1
        spans.append((lo, hi))
    return spans


@dataclass
class Statement:
    day: date
    rows: list[VesselRow]
    berthed: dict[str, int]          # terminal label -> vessels berthed to date (with 'Total')
    layout: set[str] = field(default_factory=set)


def _base(day: date, section: str, vessel_text: str, reg: str, source: str) -> dict:
    name, prefix = vessel_name(vessel_text)
    det = {"prefix": prefix} if prefix else {}
    return dict(port=PORT, statement_date=day, section=section, vessel=name, call_ref=reg, details=det,
                source_file=source)


def berth_section(g: Grid, day: date, carry: str, source: str) -> tuple[list[VesselRow], str]:
    cols = {c: i for i, c in enumerate(BERTH_COLS)}
    out: list[VesselRow] = []
    for r in range(len(g.text)):
        head = g.row_text(r)
        if "NAME OF VESSEL" in head or "UNLOADED" in head or "(IMPORT)" in head:
            continue
        berth = joined(g.cell(r, 0)) or carry
        carry = berth
        vtext = joined(g.cell(r, 1))
        if not vtext or vtext.upper().startswith("VACANT"):
            continue
        for lo, hi in ship_blocks(g, r, cols):
            cell = {c: between(g.cell(r, i), lo, hi) for c, i in cols.items() if c != "berth"}
            vessel = joined(cell["vessel"])
            if not vessel or vessel.upper().startswith("VACANT"):
                continue
            reg = next((t.replace(" ", "") for _, t in cell["reg"] if _REG.match(t.replace(" ", ""))), "")
            anchor = next((y for y, t in cell["reg"] if _REG.match(t.replace(" ", ""))), lo + ABOVE)
            bday = day_month(joined(cell["berth_date"][:1]), day) if cell["berth_date"] else None
            btime = hour_minute(joined(cell["berth_time"][:1])) if cell["berth_time"] else None
            cast = [t for _, t in cell["cast_off"]]
            cast_day = next((d for t in cast if (d := day_month(t, day + timedelta(days=7)))), None)
            cast_hm = next((hm for t in cast if (hm := hour_minute(t))), None)
            agent = joined(cell["agent"])
            base = _base(day, "berth", vessel, reg, source)
            base["details"].update({k: v for k, v in (("pilot", joined(cell["pilot"])),) if v})
            spill = [(y, t) for c in FIGURES for y, t in cell[c] if ports_pk.unit_of(t)]
            subs = sub_rows(sorted(cell["commodity"] + spill))
            flines = figure_lines({c: [(y, t) for y, t in cell[c] if not ports_pk.unit_of(t)] for c in FIGURES})
            if not flines:  # alongside, no cargo figures yet (or none to work)
                for s in subs or [None]:
                    out.append(_berth_row(base, berth, agent, cell, s, "", {}, bday, btime, cast_day, cast_hm))
                continue
            for s, direction, figs in assign(subs, flines, anchor):
                out.append(_berth_row(base, berth, agent, cell, s, direction, figs, bday, btime, cast_day, cast_hm))
    return out, carry


def _berth_row(base, berth, agent, cell, s, direction, figs, bday, btime, cast_day, cast_hm) -> VesselRow:
    vals, notes = {}, []
    for c in FIGURES:
        v, note = figure(figs.get(c, ""))
        vals[c] = v
        if note:
            notes.append(note)
    m, h, p, b = (vals[c] for c in FIGURES)
    ok = None
    if m is not None and any(v is not None for v in (h, p, b)):
        ok = abs(m - sum(v or 0 for v in (h, p, b))) <= max(1.0, m * 1e-4)
    raw = s.text if s else ""
    unit_raw = (s.unit if s else None) or ""
    name, unit = ports_pk.commodity(raw, unit_raw)
    row = VesselRow(**{**base, "details": dict(base["details"])}, direction=direction, commodity_raw=raw,
                    commodity=name, unit=unit, berth_raw=berth, terminal=ports_pk.terminal(berth),
                    agent_raw=agent or None, agent=ports_pk.agent(agent), loa_m=to_float(joined(cell["loa"][:1])),
                    manifest=m, handled_24h=h, handled_before=p, balance=b,
                    berthed_at=at(bday, btime), departs_at=at(cast_day, cast_hm) if cast_hm else None,
                    checks_ok=ok)
    if unit_raw:
        row.details["unit_raw"] = unit_raw
    if notes:
        row.details["notes"] = sorted(set(notes))
    return row


def anchorage_section(g: Grid, day: date, cols: dict[str, int], start: int, source: str) -> list[VesselRow]:
    out: list[VesselRow] = []
    for r in range(start, len(g.text)):
        head = g.row_text(r)
        if "SHIP NAME" in head or not head.strip():
            continue
        for lo, hi in ship_blocks(g, r, cols):
            cell = {c: between(g.cell(r, i), lo, hi) for c, i in cols.items()}
            vessel = joined(cell.get("vessel", []))
            if not vessel:
                continue
            reg = next((t.replace(" ", "") for _, t in cell.get("reg", []) if _REG.match(t.replace(" ", ""))), "")
            base = _base(day, "anchorage", vessel, reg, source)
            regd = hour_minute(joined(cell.get("regd", [])[:1])) if cell.get("regd") else None
            arrived = day_month(joined(cell.get("arrived", [])[:1]), day) if cell.get("arrived") else None
            if regd:
                base["details"]["registered_time"] = "%02d:%02d" % regd
            subs = sub_rows(cell.get("commodity", [])) or [SubRow(top=lo + ABOVE, name=[""])]
            starts = [s.top for s in subs]
            per: dict[int, dict[str, str]] = defaultdict(dict)
            for c in ("weight", "direction"):
                for y, t in cell.get(c, []):
                    i = max([k for k, s in enumerate(starts) if y >= s - ABOVE] or [0])
                    per[i].setdefault(c, t)
            agent, flag, lport = (joined(cell.get(k, [])) for k in ("agent", "flag", "port"))
            for i, s in enumerate(subs):
                w, _ = figure(per[i].get("weight", ""))
                d = per[i].get("direction", "").strip().lower()
                direction = "import" if d.startswith("imp") else "export" if d.startswith("exp") else ""
                name, unit = ports_pk.commodity(s.text, s.unit or "")
                row = VesselRow(**{**base, "details": dict(base["details"])}, direction=direction,
                                commodity_raw=s.text, commodity=name, unit=unit,
                                berth_raw=joined(cell.get("berth", [])) or None,
                                terminal=ports_pk.terminal(joined(cell.get("berth", []))),
                                agent_raw=agent or None, agent=ports_pk.agent(agent), flag_raw=flag or None,
                                flag=ports_pk.country(flag), port_raw=lport or None,
                                country=ports_pk.country(lport) if direction != "export" else None,
                                loa_m=to_float(joined(cell.get("loa", [])[:1])),
                                draft_m=to_float(joined(cell.get("draft", [])[:1])),
                                manifest=w, arrived_on=arrived)
                if s.unit:
                    row.details["unit_raw"] = s.unit
                out.append(row)
    return out


_EXP_DUE = re.compile(r"(\d{1,2})[-/ ]([A-Za-z]{3}|\d{1,2})[-/ ](\d{2,4})\s*(\d{1,2}[:.]?\d{2})?")


def expected_section(g: Grid, day: date, source: str) -> list[VesselRow]:
    hdr = next((i for i in range(len(g.text)) if "SHIP NAME" in g.row_text(i)), None)
    if hdr is None:
        return []
    cols = header_map(g, hdr, (("vessel", ("SHIP NAME",)), ("port", ("LAST PORT",)), ("due", ("DUE",)),
                               ("commodity", ("COMMODITY",)), ("weight", ("WEIGHT", "TONNE")),
                               ("direction", ("IMPORT/EXPORT", "IMP/EXP", "IMPORT")), ("agent", ("AGENT",))))
    if "vessel" not in cols:
        return []
    out: list[VesselRow] = []
    for r in range(hdr + 1, len(g.text)):
        vlines = [(y, t) for y, t in g.cell(r, cols["vessel"]) if t.strip()]
        if not vlines or "NO SHIPS" in g.row_text(r):
            continue
        starts = [y for y, t in vlines if _PREFIX.match(t)] or [vlines[0][0]]
        for i, a in enumerate(starts):
            lo = a - ABOVE if i else g.ys[r][0]
            hi = starts[i + 1] - ABOVE if i + 1 < len(starts) else g.ys[r][1]
            cell = {c: between(g.cell(r, k), lo, hi) for c, k in cols.items()}
            vessel = joined(cell["vessel"])
            if not vessel:
                continue
            due = None
            m = _EXP_DUE.search(joined(cell.get("due", [])))
            if m:
                mo = MONTHS.get(m.group(2)[:3].lower()) if m.group(2).isalpha() else int(m.group(2))
                y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
                try:
                    due = datetime(y, mo, int(m.group(1)), *(hour_minute(m.group(4) or "") or (0, 0)))
                except (TypeError, ValueError):
                    due = None
            subs = sub_rows(cell.get("commodity", [])) or [SubRow(top=a, name=[""])]
            d = joined(cell.get("direction", [])).lower()
            direction = "import" if d.startswith("imp") else "export" if d.startswith("exp") else ""
            w, _ = figure(joined(cell.get("weight", [])[:1]))
            agent, lport = joined(cell.get("agent", [])), joined(cell.get("port", []))
            for k, s in enumerate(subs):
                name, unit = ports_pk.commodity(s.text, s.unit or "")
                base = _base(day, "expected", vessel, "", source)
                out.append(VesselRow(**base, direction=direction, commodity_raw=s.text, commodity=name, unit=unit,
                                     agent_raw=agent or None, agent=ports_pk.agent(agent), port_raw=lport or None,
                                     country=ports_pk.country(lport), manifest=w if k == 0 else None,
                                     expected_at=due))
    return out


_SAILED = re.compile(r"^\s*\d{1,3}\s+(\d{3,6}(?:-\d{1,2})?)\s+(.+?)\s+(\S+)\s+(\d{1,2})-(\d{1,2})-(\d{4})\s+(\d{1,4})\s*$")


def sailed_section(g: Grid, day: date, source: str) -> list[VesselRow]:
    out: list[VesselRow] = []
    for r in range(len(g.text)):
        line = " ".join(c.replace("\n", " ") for c in g.text[r] if c).strip()
        m = _SAILED.match(line)
        if not m:
            continue
        hm = m.group(7).zfill(4)
        try:
            when = datetime(int(m.group(6)), int(m.group(5)), int(m.group(4)), int(hm[:2]), int(hm[2:]))
        except ValueError:
            when = None
        base = _base(day, "sailed", m.group(2), m.group(1), source)
        out.append(VesselRow(**base, berth_raw=m.group(3), terminal=ports_pk.terminal(m.group(3)), departs_at=when))
    return out


BERTHED_TERMINALS = ("M/W", "QICT", "IOCB", "FOTCO", "EVTL", "PROGAS", "QT-2", "LCT", "FAP", "SSGC", "EETL", "PIBT",
                     "OTHER", "PGPCL", "PQEPT", "HFP&S")
_TOTAL = re.compile(r"\bTotal\s+(\d{3,7})(?![\d/-])")  # PQA's total, printed below the table


def berthed_counts(gs: list[Grid], text: str) -> dict[str, int]:
    """'No. of vessels berthed' per terminal from the 2-column tables that hold it, and PQA's 'Total'
    (printed below the table, so read from the page text)."""
    out: dict[str, int] = {}
    m = _TOTAL.search(text[text.upper().rfind("BERTHED"):]) if "BERTHED" in text.upper() else None
    if m:
        out["Total"] = int(m.group(1))
    for g in gs:
        for row in g.text:
            label, value = (row[0] or "").strip(), (row[1] or "").strip().replace(",", "")
            if label and value.isdigit() and re.match(r"^[A-Za-z][A-Za-z0-9&/ .-]{1,12}$", label):
                out.setdefault(label.upper() if label.lower() != "total" else "Total", int(value))
    if out:  # a row pdfplumber merged into the header cell ('M/W' in 2023) is still in the text
        below = text[text.upper().rfind("BERTHED"):] if "BERTHED" in text.upper() else ""
        for label in BERTHED_TERMINALS:
            m = None if label in out else re.search(rf"(?<![\w-]){re.escape(label)}\s+(\d{{1,6}})(?![\d/-])", below)
            if m:
                out[label] = int(m.group(1))
    return out


def parse_pdf(content: bytes, source: str = "") -> Statement:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        texts = [p.extract_text() or "" for p in pdf.pages]
        if not texts[0].strip():
            raise ValueError("no text layer (scanned statement)")
        day = checked_date(statement_date(texts[0]))
        gs = grids(pdf)
        tail = "\n".join(texts[-2:])
    rows: list[VesselRow] = []
    layout: set[str] = set()
    carry = ""
    anch_cols: dict[str, int] | None = None
    two_col: list[Grid] = []
    prev: tuple[str, int] | None = None
    for g in gs:
        k = kind(g, prev)
        if k:
            prev = (k, len(g.text[0]))
        if k == "berth":
            got, carry = berth_section(g, day, carry, source)
            rows += got
            layout.add("berth")
        elif k == "anchorage":
            hdr = next((i for i in range(min(2, len(g.text))) if "SHIP NAME" in g.row_text(i)), None)
            cols = anch_cols
            if hdr is not None:
                anch_cols = cols = header_map(g, hdr)
                layout.add("anchorage:" + ",".join(sorted(anch_cols)))
            elif anch_cols and len(g.text[0]) != max(anch_cols.values()) + 1 and len(g.text[0]) == 13:
                # a continuation without the 14-column header's empty column: the plain 13-column order
                style = "loa_first" if anch_cols.get("loa") == 0 else "terminal_first"
                cols = {n: i for i, n in enumerate(ANCHORAGE_LAYOUTS[style]) if n}
            if cols and "vessel" in cols and max(cols.values()) < len(g.text[0]):
                rows += anchorage_section(g, day, cols, 0 if hdr is None else hdr + 1, source)
        elif k == "expected":
            rows += expected_section(g, day, source)
            layout.add("expected")
        elif k == "sailed":
            rows += sailed_section(g, day, source)
            layout.add("sailed")
        elif k == "berthed":
            two_col.append(g)
    if "berth" not in layout:
        raise ValueError("no berth-wise table found")
    if any("OUTER ANCHORAGE" in t.upper() for t in texts):
        layout.add("anchorage_heading")  # an empty anchorage prints its heading and no table
    return Statement(day=day, rows=number_seqs(rows), berthed=berthed_counts(two_col, tail), layout=layout)


# ---------------------------------------------------------------------------------------------------------
# derived series

_C = dict(module="external", frequency="daily", source="PQA", source_url=SOURCE_URL, min_value=0)
_DIR = ["all", "import", "export"]
_AT = "as at 08:00 each day (Port Qasim Authority's daily shipping and cargo statement, May 2019 →)"
CATALOG = [
    dict(id="ports.qasim.cargo_handled", name="Port Qasim cargo handled (24 hours)", unit="metric ton",
         max_value=2e6, dimensions={"level": ["total", "commodity", "terminal"], "direction": _DIR},
         description=f"Tonnes of cargo loaded and unloaded at Port Qasim's berths in the 24 hours to 08:00, by "
                     f"direction (dimension direction: all, import, export) and by commodity or terminal (dimension "
                     f"level: total, commodity — with commodity —, terminal — with terminal), {_AT}. Containers "
                     f"and LNG are counted in their own series.", **_C),
    dict(id="ports.qasim.containers_handled", name="Port Qasim containers handled (TEUs, 24 hours)", unit="teu",
         max_value=2e5, dimensions={"level": ["total", "terminal"], "direction": _DIR},
         description=f"Twenty-foot equivalent units (TEUs) loaded and unloaded at Port Qasim's container terminals "
                     f"in the 24 hours to 08:00, by direction and terminal, {_AT}. (PQA prints no unit; the daily "
                     f"figures add up to its annual TEU totals.)", **_C),
    dict(id="ports.qasim.lng_handled", name="Port Qasim LNG discharged (24 hours)", unit="cubic metre",
         max_value=5e6, dimensions={"level": ["total", "terminal"]},
         description=f"Cubic metres of liquefied natural gas discharged at Port Qasim's two LNG terminals in the "
                     f"24 hours to 08:00, by terminal, {_AT}.", **_C),
    dict(id="ports.qasim.ships_at_berth", name="Ships at berth at Port Qasim", unit="count", max_value=200,
         dimensions={"level": ["total", "terminal"]},
         description=f"Ships alongside Port Qasim's berths, by terminal, {_AT}.", **_C),
    dict(id="ports.qasim.ships_at_anchorage", name="Ships waiting at Port Qasim's outer anchorage", unit="count",
         max_value=500, dimensions={"level": ["total", "commodity", "loading_country"], "direction": _DIR},
         description=f"Ships waiting at Port Qasim's outer anchorage for a berth, by direction of their cargo and by "
                     f"commodity or loading country (dimension level: total, commodity, loading_country — with "
                     f"country), {_AT}. A ship with two cargoes counts once in the total.", **_C),
    dict(id="ports.qasim.cargo_at_anchorage", name="Cargo waiting at Port Qasim's outer anchorage",
         unit="metric ton", max_value=2e7,
         dimensions={"level": ["total", "commodity", "loading_country"], "direction": _DIR},
         description=f"Tonnes of cargo aboard the ships waiting at Port Qasim's outer anchorage, by direction and "
                     f"by commodity or loading country, {_AT}. Containers and LNG (counted in other units) are "
                     f"left out.", **_C),
    dict(id="ports.qasim.expected_arrivals", name="Ships expected at Port Qasim", unit="count", max_value=500,
         dimensions={"direction": _DIR},
         description=f"Ships Port Qasim lists as expected to arrive, by direction of their cargo, {_AT}.", **_C),
    dict(id="ports.qasim.berthings_to_date", name="Vessels berthed at Port Qasim since opening", unit="count",
         max_value=1e6, dimensions={"level": ["total", "terminal"]},
         description=f"PQA's running count of vessels berthed at each terminal since it opened, and the port's "
                     f"total; the day-to-day change is the number of ships berthed. {_AT}.", **_C),
]
UNIT_SERIES = {"metric ton": "ports.qasim.cargo_handled", "teu": "ports.qasim.containers_handled",
               "cubic metre": "ports.qasim.lng_handled"}


def _rec(out: dict, sid: str, day: date, dims: dict, value: float) -> None:
    k = (sid, tuple(sorted(dims.items())))
    out[k] = Record(sid, day, value, dims) if k not in out else Record(sid, day, out[k].value + value, dims)


def to_records(st: Statement) -> list[Record]:
    """The day's series from its vessel rows (none for a statement before the album's span — the 2002
    reprint — whose ships go to the vessel table only, so the daily series don't start with a lone point
    seventeen years early). Pure."""
    acc: dict = {}
    day = st.day
    if day < FIRST_DAY:
        return []
    berth = [r for r in st.rows if r.section == "berth"]
    for sid in UNIT_SERIES.values():  # a day with no work still reads 0, not missing
        _rec(acc, sid, day, {"level": "total", "direction": "all"} if sid != "ports.qasim.lng_handled"
             else {"level": "total"}, 0.0)
    for r in berth:
        sid = UNIT_SERIES.get(r.unit or "")
        if not sid or r.checks_ok is False or r.handled_24h is None or r.direction not in ("import", "export"):
            continue
        v = r.handled_24h
        if sid == "ports.qasim.lng_handled":
            _rec(acc, sid, day, {"level": "total"}, v)
            if r.terminal:
                _rec(acc, sid, day, {"level": "terminal", "terminal": r.terminal}, v)
            continue
        for d in ("all", r.direction):
            _rec(acc, sid, day, {"level": "total", "direction": d}, v)
            if r.terminal:
                _rec(acc, sid, day, {"level": "terminal", "direction": d, "terminal": r.terminal}, v)
            if sid == "ports.qasim.cargo_handled" and r.commodity:
                _rec(acc, sid, day, {"level": "commodity", "direction": d, "commodity": r.commodity}, v)
    ships = {(r.vessel, r.call_ref): r.terminal for r in berth}
    _rec(acc, "ports.qasim.ships_at_berth", day, {"level": "total"}, float(len(ships)))
    by_term: dict[str, int] = defaultdict(int)
    for t in ships.values():
        if t:
            by_term[t] += 1
    for t, n in by_term.items():
        _rec(acc, "ports.qasim.ships_at_berth", day, {"level": "terminal", "terminal": t}, float(n))
    # anchorage: ships counted once per group, tonnage summed over tonnage lines
    anch = [r for r in st.rows if r.section == "anchorage"]
    groups: dict[tuple, set] = defaultdict(set)
    for r in anch:
        ship = (r.vessel, r.call_ref)
        dirs = ["all"] + ([r.direction] if r.direction in ("import", "export") else [])
        for d in dirs:
            keys = [(("level", "total"), ("direction", d))]
            if r.commodity:
                keys.append((("level", "commodity"), ("direction", d), ("commodity", r.commodity)))
            if r.country and r.direction == "import":
                keys.append((("level", "loading_country"), ("direction", d), ("country", r.country)))
            for k in keys:
                groups[k].add(ship)
                if r.unit == "metric ton" and r.manifest:
                    _rec(acc, "ports.qasim.cargo_at_anchorage", day, dict(k), r.manifest)
    if not anch and "anchorage_heading" not in st.layout:
        groups.clear()  # no anchorage section read: no figure, rather than a false zero
    else:
        groups.setdefault((("level", "total"), ("direction", "all")), set())
    for k, s in groups.items():
        _rec(acc, "ports.qasim.ships_at_anchorage", day, dict(k), float(len(s)))
    if groups and not any(k[0] == "ports.qasim.cargo_at_anchorage" for k in acc):
        _rec(acc, "ports.qasim.cargo_at_anchorage", day, {"level": "total", "direction": "all"}, 0.0)
    if "expected" in st.layout:
        exp = [r for r in st.rows if r.section == "expected"]
        _rec(acc, "ports.qasim.expected_arrivals", day, {"direction": "all"}, float(len({r.vessel for r in exp})))
        for d in ("import", "export"):
            n = len({r.vessel for r in exp if r.direction == d})
            if n:
                _rec(acc, "ports.qasim.expected_arrivals", day, {"direction": d}, float(n))
    b = st.berthed
    terms = {k: v for k, v in b.items() if k != "Total"}
    # PQA's printed total must match its terminals; where the total line can't be read, every terminal
    # must be there instead
    total = b.get("Total") if "Total" in b else (sum(terms.values()) if set(BERTHED_TERMINALS) <= set(terms) else None)
    if total is not None and terms and sum(terms.values()) == total:
        _rec(acc, "ports.qasim.berthings_to_date", day, {"level": "total"}, float(total))
        for k, v in terms.items():
            _rec(acc, "ports.qasim.berthings_to_date", day,
                 {"level": "terminal", "terminal": ports_pk.terminal(k) or k}, float(v))
    elif b:
        log.warning("pqa_shipping: %s berthed counts don't add up to the total — skipped", day)
    return list(acc.values())


# ---------------------------------------------------------------------------------------------------------
# job

def _chain_key(r: VesselRow) -> tuple:
    return (r.vessel, r.call_ref, r.direction, r.commodity_raw, r.seq)


def chain(st: Statement, days: dict[date, dict]) -> None:
    """Check each berth line against the statement a day before and a day after (whichever were read in
    this run): a ship's 'previous' figure must equal the day before's previous + last 24 hours. A line
    that chains is confirmed (checks_ok True where its own manifest couldn't check it — container lines
    print none); one that doesn't is marked in details['chain'] (PQA restated the day before: its round
    '500' became the real count). `days` maps statement date -> {key: row}; updated in place."""
    mine = {_chain_key(r): r for r in st.rows if r.section == "berth" and r.direction}
    days[st.day] = mine
    for prev, cur in ((days.get(st.day - timedelta(days=1)), mine), (mine, days.get(st.day + timedelta(days=1)))):
        if not prev or not cur:
            continue
        for k, r in cur.items():
            p = prev.get(k)
            if p is None or r.handled_before is None or p.checks_ok is False or r.checks_ok is False:
                continue
            want = (p.handled_before or 0) + (p.handled_24h or 0)
            if abs(want - r.handled_before) <= 1:
                r.details["chain"] = "ok"
                for x in (p, r):
                    if x.checks_ok is None:
                        x.checks_ok = True
            else:
                r.details["chain"] = f"previous {r.handled_before:g}; the day before implied {want:g}"


WAYBACK = "https://web.archive.org/web/{}id_/{}"
RECOVERED_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "reference", "pqa_recovered_statements.json")


def recovered() -> list[dict]:
    """Statements PQA once listed but has taken out of its album, found in the Internet Archive (captures
    of the PDFs and of the album page itself): [{url, wayback timestamp, date}]. Fetched from PQA first
    (most are still on its server), else from the archived copy."""
    with open(RECOVERED_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def album_links(page: str) -> list[str]:
    """Statement PDF urls on an album page, in page order (newest first). Pure."""
    seen: list[str] = []
    for u in _LINK.findall(page):
        if u not in seen:
            seen.append(u)
    return seen


def last_page(page: str) -> int:
    return max([int(p) for p in _PAGE.findall(page)] or [1])


class PqaShippingJob(PortCallsMixin, IngestionJob):
    name = "pqa_shipping"
    source = "PQA"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        self._totals: dict[date, int | None] = {}
        self._days: dict[date, dict] = {}
        first = self.http_get(ALBUM_URL).decode("utf-8", "replace")
        links = album_links(first)
        if not links:
            raise ValueError("no statements linked on the PQA album page")
        wayback: dict[str, str] = {}
        if backfill:
            for p in range(2, last_page(first) + 1):
                time.sleep(BACKFILL_PAUSE_S)
                page = self.http_get(f"{ALBUM_URL}?page={p}").decode("utf-8", "replace")
                links += [u for u in album_links(page) if u not in links]
            # statements PQA has since taken out of the album (mostly June 2022 → February 2023)
            for item in recovered():
                if item["url"] not in links:
                    links.append(item["url"])
                    wayback[item["url"]] = item["wayback"]
        else:
            links = links[:LATEST_N]
        out: list[FetchedFile] = []
        errors: list[Exception] = []
        for i, url in enumerate(reversed(links)):  # oldest first: the newest re-upload of a day wins
            if backfill and i:
                time.sleep(BACKFILL_PAUSE_S)
            content = b""
            for src in [url.replace(" ", "%20")] + ([WAYBACK.format(wayback[url], url)] if url in wayback else []):
                try:
                    content = self.http_get(src)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
                    continue
                if content[:4] == b"%PDF":
                    break
            if content[:4] == b"%PDF":
                out.append(FetchedFile(filename=url.rsplit("/", 1)[-1], content=content, when=date.today()))
        if errors and not out:
            raise errors[0]
        return out

    def parse(self, f: FetchedFile) -> list:
        st = parse_pdf(f.content, f.filename)
        # PQA re-uploads a day now and then (same figures: the later copy wins) but once printed an old
        # date over another day's figures; its running berthing count gives it away
        total, seen = st.berthed.get("Total"), self.__dict__.setdefault("_totals", {})
        if total and seen.get(st.day) and seen[st.day] != total:
            raise ValueError(f"a second statement dated {st.day} counts {total} vessels berthed, not "
                             f"{seen[st.day]}: another day's figures under a stale date")
        seen.setdefault(st.day, total)
        chain(st, self.__dict__.setdefault("_days", {}))
        return st.rows + to_records(st)
