"""wapda_river_flows - WAPDA's daily river flows, reservoir levels and Kotri escapage, 2015 → 2024.

Source: Water and Power Development Authority, "River Flow" — https://wapda.gov.pk/river-flow/
embeds one workbook, https://wapda.gov.pk/wp-content/uploads/2024/12/GRAPH-DG-16-for-MAIL-2.xls,
uploaded 20 December 2024 and not updated since (checked 2026-10-31: the data end on 19 Dec 2024;
the server's Last-Modified is a later re-copy of the same bytes). Its sheets:

- GHRAPH: one row a day from 1 Mar 2016 — Indus at Tarbela level (ft) / inflow / outflow, Kabul
  inflow at Nowshera, Jhelum at Mangla level / inflow / outflow, Chenab inflow at Marala, and the
  system inflow (the four rim-station inflows summed) for the current year, the same day last year
  and WAPDA's ten-year average for the day — flows in thousands of cusecs. Rows after the last
  reading (to 31 Mar 2025) carry only the last-year and ten-year columns; they are kept.
- Kotri: escapage below Kotri barrage (the water reaching the sea), cusecs, one column per month of
  each Rabi (Oct–Mar) / Kharif (Apr–Sep) season side by side, a row per day of the month.
- Temp: daily maximum and minimum temperature (°C) at seven stations of Tarbela's catchment
  (Skardu, Besham, Tarbela, Phulra, Daggar, Shinkiari, Oghi) from 1 Apr 2017. Dates are a date cell
  on the 1st/11th/21st and the day number otherwise; some date cells are mistyped (2017-04-11 for
  the 21st, 2018-06-12 for the 21st) and one day is skipped (9 Oct 2021), so a row's day comes from
  its day number and only the month and year from the date cells.

History before the workbook: WAPDA's old site printed the same tables as web pages, captured by the
Internet Archive (`WAYBACK`): "River Flows 2015" and "2016" (calendar years), the rolling table of
2017 → (28 captures, Apr 2017 – Feb 2023) and the Kotri grid (14, Kharif 2017 → Rabi 2022-23). The
web tables give the day and month only ("6-Feb"), newest first; the year comes from the capture
date (or the "YEAR 2015" heading) and steps back as the months wrap. `--backfill` reads every
capture oldest → newest and the workbook last, so the newest print of each day is the one stored;
the earlier prints are the vintages (`scripts/vintages_wapda.py` → `scripts/vintages_core.py`).
Nothing older than January 2015 is on WAPDA's site or in the archive.

Quirks kept as published: Kabul at Nowshera reads 0.0 on 30 Apr 2016 and 10 Oct 2017 and Chenab at
Marala 0.0 on 23 Jan 2021 in the workbook (the 2017–2019 web tables printed 25.0 and 6.2 for the
first two, kept as earlier prints); Tarbela's level reads 1150.00 ft on 21 Aug 2018 in every copy
(1550.00 the day before and after).
"Last year" on 29 February reads 0 (no such day a year earlier) and is left out. The 2015 table prints
Mangla's level on 8 Feb 2015 as 11554.95 ft; the catalog bound (1,600 ft) rejects it, so that day has no
Mangla level.

Series (dims `site`, `river`, `measure`), dated as on the day of the reading:
    water.wapda.river_flow           cusecs — inflow (Tarbela, Nowshera, Mangla, Marala),
                                     outflow (Tarbela, Mangla, Kotri: escapage below the barrage)
    water.wapda.reservoir_level      feet — Tarbela, Mangla
    water.wapda.system_inflow        cusecs — measure current | last_year | ten_year_average
    water.wapda.catchment_temperature °C — dims station, measure max | min
wapda.gov.pk answers the owner's PC but not the VPS. The workbook is frozen, so the job runs from
the PC (`ops/deploy/load_from_pc.sh wapda_river_flows --backfill --commit`) and is not scheduled;
if WAPDA resumes publishing, see docs/ingestion.md (probe workflow `wapda_probe.yml`).
"""
from __future__ import annotations

import hashlib
import html as htmllib
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

from ingestion import water_pk
from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

PAGE_URL = "https://wapda.gov.pk/river-flow/"
WORKBOOK_URL = "https://wapda.gov.pk/wp-content/uploads/2024/12/GRAPH-DG-16-for-MAIL-2.xls"
WORKBOOK_PUBLISHED = date(2024, 12, 20)  # WordPress media 1178, uploaded with the "River Flow" page
WORKBOOK_SHA = "5b163cbd4166f39e"         # sha256 prefix of that upload (unchanged as of 2026-10-31)
WAYBACK_PAUSE_S = 2.0

_OLD = "http://www.wapda.gov.pk:80/index.php/"
_RFP = _OLD + "river-flow-in-pakistan?tmpl=component&id="
# Internet Archive captures of WAPDA's old river-flow pages (kind, capture timestamp, page), oldest
# first. One capture per distinct content (CDX collapse=digest); empty or error captures left out.
WAYBACK: list[tuple[str, str, str]] = [
    ("flows", "20170423014212", _RFP + "201"),     # River Flows 2016 (calendar year)
    ("flows", "20170501214120", _RFP + "120"),     # River Flows 2015 (calendar year)
    ("flows", "20170501215350", _RFP + "42"),
    ("flows", "20170808174806", _RFP + "201"),
    ("flows", "20170808180010", _RFP + "120"),
    ("flows", "20170808183035", _RFP + "42"),
    ("kotri", "20171220045554", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("kotri", "20180119124304", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("kotri", "20180220050259", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("kotri", "20180322235616", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("kotri", "20180423144554", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("kotri", "20180525083726", _OLD + "river-flow-in-pakistan/17-hydro/60-below-kotri?"),
    ("flows", "20190208191135", _OLD + "river-flow-data"),
    ("flows", "20190311223836", _OLD + "river-flow-data"),
    ("flows", "20190411192210", _OLD + "river-flow-data"),
    ("flows", "20190513084352", _OLD + "river-flow-data"),
    ("flows", "20190613025313", _OLD + "river-flow-data"),
    ("flows", "20190706181554", _RFP + "201"),
    ("flows", "20190706181555", _RFP + "120"),
    ("flows", "20190706181600", _RFP + "42"),
    ("flows", "20190719122532", _OLD + "river-flow-data"),
    ("flows", "20190819235728", _OLD + "river-flow-data"),
    ("flows", "20190919212807", _OLD + "river-flow-data"),
    ("flows", "20191019115938", _OLD + "river-flow-data"),
    ("flows", "20191118191937", _OLD + "river-flow-data"),
    ("kotri", "20191120215120", _RFP + "60"),
    ("flows", "20191123071455", _RFP + "42"),
    ("flows", "20191218225615", _OLD + "river-flow-data"),
    ("flows", "20200118032820", _OLD + "river-flow-data"),
    ("kotri", "20200120035653", _RFP + "60"),
    ("flows", "20200122132505", _RFP + "42"),
    ("flows", "20200217054354", _OLD + "river-flow-data"),
    ("kotri", "20200928031552", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("flows", "20201001122957", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("flows", "20210225161533", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("kotri", "20210225170335", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("flows", "20210610002632", "http://www.wapda.gov.pk/index.php/river-flow-data"),
    ("flows", "20210728081510", "http://www.wapda.gov.pk/index.php/river-flow-data?tmpl=component&print=1&page="),
    ("flows", "20210728081734", "http://www.wapda.gov.pk/index.php/river-flow-data?tmpl=component&print=1&page="),
    ("flows", "20211203041146", "http://wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("kotri", "20211203050256", "http://wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("flows", "20220705055225", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("flows", "20220822051914", "https://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("kotri", "20220822051919", "https://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("flows", "20220823051100", "http://wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
    ("kotri", "20220823051104", "http://wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("kotri", "20230127221112", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=60"),
    ("flows", "20230203200145", "http://www.wapda.gov.pk/index.php/river-flow-in-pakistan?tmpl=component&id=42"),
]

FLOW = "water.wapda.river_flow"
LEVEL = "water.wapda.reservoir_level"
SYSTEM = "water.wapda.system_inflow"
TEMP = "water.wapda.catchment_temperature"

# The eleven value columns of the river-flow table (workbook and web pages alike), left to right:
# (series, dims, decimals WAPDA prints, scale to our unit). Flows are printed in thousand cusecs.
_T = {"site": "Tarbela", "river": "Indus"}
_M = {"site": "Mangla", "river": "Jhelum"}
COLUMNS: list[tuple[str, dict, int, float]] = [
    (LEVEL, _T, 2, 1.0),
    (FLOW, {**_T, "measure": "inflow"}, 1, 1000.0),
    (FLOW, {**_T, "measure": "outflow"}, 1, 1000.0),
    (FLOW, {"site": "Nowshera", "river": "Kabul", "measure": "inflow"}, 1, 1000.0),
    (LEVEL, _M, 2, 1.0),
    (FLOW, {**_M, "measure": "inflow"}, 1, 1000.0),
    (FLOW, {**_M, "measure": "outflow"}, 1, 1000.0),
    (FLOW, {"site": "Marala", "river": "Chenab", "measure": "inflow"}, 1, 1000.0),
    (SYSTEM, {"measure": "current"}, 1, 1000.0),
    (SYSTEM, {"measure": "last_year"}, 1, 1000.0),
    (SYSTEM, {"measure": "ten_year_average"}, 1, 1000.0),
]
# the group headings above them, in order — a layout change fails the parse instead of mislabelling
_HEADINGS = ("TARBELA", "NOWSHERA", "MANGLA", "MARALA", "SYSTEM")
_KOTRI = (FLOW, {"site": "Kotri", "river": "Indus", "measure": "outflow"}, 0, 1.0)

_C = dict(module="real", frequency="daily", source="WAPDA", source_url=PAGE_URL)
_G = ("as published by the Water and Power Development Authority (WAPDA), one value a day, January "
      "2015 to 19 December 2024 (WAPDA has not updated its river-flow data since)")
CATALOG = [
    dict(id=FLOW, name="River flow at Tarbela, Mangla, Nowshera, Marala and Kotri (WAPDA)", unit="cusecs",
         min_value=0, max_value=2e6,
         dimensions={"site": ["Tarbela", "Mangla", "Nowshera", "Marala", "Kotri"], "measure": ["inflow", "outflow"]},
         description=f"Daily mean flow in cusecs (cubic feet per second) {_G}: Indus inflow and outflow at "
                     f"Tarbela, Kabul inflow at Nowshera, Jhelum inflow and outflow at Mangla, Chenab inflow "
                     f"at Marala, and the escapage below Kotri barrage (outflow — the Indus water reaching "
                     f"the sea; April 2017 → January 2023 and October 2023 →). Dimensions site, river, measure.", **_C),
    dict(id=LEVEL, name="Tarbela and Mangla reservoir level (WAPDA)", unit="ft", min_value=1000, max_value=1600,
         dimensions={"site": ["Tarbela", "Mangla"]},
         description=f"Water level of the Tarbela (Indus) and Mangla (Jhelum) reservoirs in feet above sea "
                     f"level, {_G}. Dimensions site, river.", **_C),
    dict(id=SYSTEM, name="Indus river system inflow at the rim stations (WAPDA)", unit="cusecs",
         min_value=0, max_value=2e6,
         dimensions={"measure": ["current", "last_year", "ten_year_average"]},
         description=f"The four rim-station inflows summed — Indus at Tarbela, Kabul at Nowshera, Jhelum at "
                     f"Mangla, Chenab at Marala — in cusecs, {_G}. Dimension measure: current (that day), "
                     f"last_year (the same day a year earlier, as WAPDA printed it alongside) and "
                     f"ten_year_average (WAPDA's average for that day over the previous ten years).", **_C),
    dict(id=TEMP, name="Tarbela catchment temperatures (WAPDA)", unit="celsius", min_value=-40, max_value=55,
         dimensions={"station": list(water_pk.CATCHMENT_STATIONS), "measure": ["max", "min"]},
         description="Daily maximum and minimum air temperature (°C) at seven weather stations in the "
                     "catchment of Tarbela dam — Skardu, Besham, Tarbela, Phulra, Daggar, Shinkiari and Oghi — "
                     "whose snow and glacier melt drives the Indus inflow, as tracked by WAPDA, April 2017 to "
                     "19 December 2024. Dimensions station, measure (max | min).",
         **{**_C, "source_url": PAGE_URL}),
]

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


@dataclass(frozen=True)
class Print:
    """One figure as WAPDA printed it: in its own unit, with the decimals it shows."""
    series_id: str
    dims: tuple                    # sorted (key, value) pairs
    obs_date: date
    value: float
    dp: int
    scale: float                   # publisher's unit -> ours


def _print(col: tuple[str, dict, int, float], when: date, v: float) -> Print:
    sid, dims, dp, scale = col
    return Print(sid, tuple(sorted(dims.items())), when, round(v, 4), dp, scale)


def to_records(prints: list[Print]) -> list[Record]:
    return [Record(p.series_id, p.obs_date, round(p.value * p.scale, 4), dict(p.dims)) for p in prints]


def _keep(col: tuple, when: date, v: float | None) -> bool:
    if v is None:
        return False
    # "last year" on 29 February: there was no such day a year earlier (WAPDA prints 0)
    return not (col[1].get("measure") == "last_year" and when.month == 2 and when.day == 29 and v == 0)


def _check_headings(text: str) -> None:
    up = text.upper()
    pos = [up.find(h) for h in _HEADINGS]
    if -1 in pos or pos != sorted(pos):
        raise ValueError(f"river-flow table headings changed: {text[:160]!r}")


# ---- the workbook --------------------------------------------------------------------------

def _cell(sh, r: int, c: int):
    if c >= sh.ncols:
        return None
    cell = sh.cell(r, c)
    return cell.value if cell.ctype == 2 else None  # XL_CELL_NUMBER


def graph_sheet(sh, datemode: int) -> list[Print]:
    """GHRAPH: a dated row per day, the eleven COLUMNS after the date."""
    import xlrd

    _check_headings(" ".join(str(sh.cell(r, c).value) for r in range(min(6, sh.nrows)) for c in range(sh.ncols)))
    out: list[Print] = []
    for r in range(sh.nrows):
        if sh.cell(r, 0).ctype != xlrd.XL_CELL_DATE:
            continue
        when = xlrd.xldate_as_datetime(sh.cell(r, 0).value, datemode).date()
        for k, col in enumerate(COLUMNS, start=1):
            v = _cell(sh, r, k)
            if _keep(col, when, v):
                out.append(_print(col, when, v))
    return out


def season_dates(label: str, month: str) -> tuple[int, int] | None:
    """('RABI 2023-24', 'JAN') -> (2024, 1); ('KHARIF 2024', 'JUL') -> (2024, 7). Pure."""
    m = re.search(r"(RABI|KHARIF)\s*(\d{4})", label, re.I)
    mo = MONTHS.get(month.strip()[:3].lower())
    if not m or not mo:
        return None
    y = int(m.group(2))
    return (y + 1 if m.group(1).upper() == "RABI" and mo <= 3 else y), mo


def kotri_grid(seasons: list[str], months: list[str], days: list[tuple[int, list]]) -> list[Print]:
    """A Kotri escapage grid -> prints. `seasons` and `months` label each value column; `days` is
    [(day of month, [value or None per column])]. Pure."""
    out: list[Print] = []
    for day, vals in days:
        for k, v in enumerate(vals):
            if v is None or k >= len(months):
                continue
            ym = season_dates(seasons[k], months[k])
            if not ym:
                continue
            try:
                when = date(ym[0], ym[1], day)
            except ValueError:
                continue  # 30 February
            out.append(_print(_KOTRI, when, v))
    return out


def kotri_sheet(sh) -> list[Print]:
    head = next((r for r in range(sh.nrows) if str(sh.cell(r, 0).value).strip().lower() == "date"), None)
    if head is None or head + 1 >= sh.nrows:
        raise ValueError("Kotri sheet: no 'Date' heading")
    seasons, cur = [], ""
    for c in range(1, sh.ncols):
        cur = str(sh.cell(head, c).value).strip() or cur
        seasons.append(cur)
    months = [str(sh.cell(head + 1, c).value) for c in range(1, sh.ncols)]
    days = []
    for r in range(head + 2, sh.nrows):
        d = _cell(sh, r, 0)
        if d is None or not 1 <= d <= 31:
            continue
        days.append((int(d), [_cell(sh, r, c) for c in range(1, sh.ncols)]))
    return kotri_grid(seasons, months, days)


def temp_sheet(sh, datemode: int) -> list[Print]:
    """Temp: station pairs (max, min); the date from the day number and the last date cell's month."""
    import xlrd

    head = next((r for r in range(min(10, sh.nrows)) if str(sh.cell(r, 0).value).strip().lower() == "date"), None)
    if head is None:
        raise ValueError("Temp sheet: no 'Date' heading")
    cols: list[tuple[int, str]] = []
    for c in range(1, sh.ncols):
        st = water_pk.canon_station(str(sh.cell(head, c).value))
        if st:
            cols.append((c, st))
    if len(cols) < 5:
        raise ValueError(f"Temp sheet: stations not recognised ({len(cols)})")
    out: list[Print] = []
    prev: date | None = None
    for r in range(head + 1, sh.nrows):
        c0 = sh.cell(r, 0)
        if c0.ctype == xlrd.XL_CELL_DATE:
            d = xlrd.xldate_as_datetime(c0.value, datemode).date()
            # a date cell inside the month we're in only confirms the month (some are mistyped)
            when = prev + timedelta(days=1) if prev and (d.year, d.month) == (prev.year, prev.month) else d
        elif c0.ctype == 2 and prev and 1 <= c0.value <= 31:
            day = int(c0.value)
            if day <= prev.day:
                raise ValueError(f"Temp sheet row {r}: day {day} after {prev}")
            when = prev.replace(day=day)
        else:
            continue
        for c, st in cols:
            for k, measure in ((c, "max"), (c + 1, "min")):
                v = _cell(sh, r, k)
                if v is not None:
                    out.append(Print(TEMP, (("measure", measure), ("station", st)), when, round(v, 2), 1, 1.0))
        prev = when
    return out


def workbook_prints(content: bytes) -> list[Print]:
    import xlrd

    book = xlrd.open_workbook(file_contents=content)
    names = {s.name.strip().lower(): s for s in book.sheets()}
    graph = names.get("ghraph") or names.get("graph")
    if graph is None:
        raise ValueError(f"no GHRAPH sheet (sheets: {list(names)})")
    out = graph_sheet(graph, book.datemode)
    if "kotri" in names:
        out += kotri_sheet(names["kotri"])
    if "temp" in names:
        out += temp_sheet(names["temp"], book.datemode)
    return out


# ---- the old web pages (Internet Archive) ------------------------------------------------------

def table_rows(page: str) -> list[list[str]]:
    """Every <tr> of a page as its cells' text, a cell with colspan=n repeated n times. Pure."""
    rows: list[list[str]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        cells: list[str] = []
        for attrs, body in re.findall(r"<t[dh]([^>]*)>(.*?)</t[dh]>", tr, re.S | re.I):
            text = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
            span = re.search(r"colspan\s*=\s*\"?(\d+)", attrs, re.I)
            cells += [text] * (int(span.group(1)) if span else 1)
        if any(cells):
            rows.append(cells)
    return rows


def _num(s: str) -> float | None:
    s = s.replace(",", "").strip()
    return float(s) if re.fullmatch(r"-?\d+(?:\.\d+)?", s) else None


def _decimals(s: str) -> int:
    return len(s.split(".", 1)[1]) if "." in s else 0


def web_flows(page: str, captured: date) -> list[Print]:
    """An old river-flow web table -> prints. Rows run newest first with day and month only."""
    rows = table_rows(page)
    _check_headings(" ".join(" ".join(r) for r in rows[:4]))
    fixed = re.search(r"YEAR\s+(\d{4})", " ".join(" ".join(r) for r in rows[:2]))
    out: list[Print] = []
    prev: date | None = None
    seen: dict[date, list[str]] = {}
    for r in rows:
        m = re.match(r"^(\d{1,2})\s*[-/ ]\s*([A-Za-z]{3})", r[0]) if r else None
        if not m or len(r) < 1 + len(COLUMNS) or m.group(2).lower() not in MONTHS:
            continue
        d, mo = int(m.group(1)), MONTHS[m.group(2).lower()]
        if fixed:
            y = int(fixed.group(1))
        elif prev is None:
            y = captured.year if (mo, d) <= (captured.month, captured.day) else captured.year - 1
        else:
            y = prev.year - 1 if (mo, d) > (prev.month, prev.day) else prev.year
        try:
            when = date(y, mo, d)
        except ValueError:
            continue
        if prev and when >= prev:
            # the YEAR 2017 page repeats its 6-Apr row after 1-Jan: a copy of a row already read
            if seen.get(when) == r[1:1 + len(COLUMNS)]:
                continue
            raise ValueError(f"web table not newest-first at {r[0]} ({when} after {prev})")
        prev = when
        seen[when] = r[1:1 + len(COLUMNS)]
        for col, raw in zip(COLUMNS, r[1:1 + len(COLUMNS)]):
            v = _num(raw)
            if _keep(col, when, v):
                sid, dims, _dp, scale = col
                out.append(Print(sid, tuple(sorted(dims.items())), when, v, _decimals(raw), scale))
    if not out:
        raise ValueError("no dated rows in the web table")
    return out


def web_kotri(page: str) -> list[Print]:
    rows = table_rows(page)
    head = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "date"), None)
    if head is None or head + 1 >= len(rows):
        raise ValueError("Kotri web table: no 'Date' heading")
    labels = [c for c in rows[head][1:] if c.strip()]
    months = [c for c in rows[head + 1] if c.strip()]
    if len(labels) == 2 and len(months) == 12:
        seasons = [labels[0]] * 6 + [labels[1]] * 6  # two seasons side by side, six months each
    elif len(labels) == len(months):
        seasons = labels                             # colspans expanded
    else:
        raise ValueError(f"Kotri web table: {len(labels)} season labels for {len(months)} months")
    days = []
    for r in rows[head + 2:]:
        d = _num(r[0]) if r else None
        if d is None or not 1 <= d <= 31 or len(r) < 2:
            continue
        days.append((int(d), [_num(c) for c in r[1:1 + len(months)]]))
    out = kotri_grid(seasons, months, days)
    if not out:
        raise ValueError("no values in the Kotri web table")
    return out


def workbook_sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()[:16]


def file_prints(f: FetchedFile) -> tuple[date, list[Print]]:
    """(published, prints) for one fetched copy: a web capture (published on its capture date) or
    the workbook — the 20 Dec 2024 upload, or a new copy dated the day we fetched it."""
    if f.filename.startswith("wayback_"):
        _, kind, ts = f.filename.rsplit(".", 1)[0].split("_")
        captured = date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))
        page = f.content.decode("utf-8", "replace")
        return captured, (web_kotri(page) if kind == "kotri" else web_flows(page, captured))
    return (WORKBOOK_PUBLISHED if workbook_sha(f.content) == WORKBOOK_SHA else f.when), workbook_prints(f.content)


class WapdaRiverFlowsJob(IngestionJob):
    name = "wapda_river_flows"
    source = "WAPDA"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        if backfill:
            for i, (kind, ts, url) in enumerate(WAYBACK):
                if i:
                    time.sleep(WAYBACK_PAUSE_S)
                try:
                    content = self.http_get(f"https://web.archive.org/web/{ts}id_/{url}")
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: web archive capture %s unavailable (%s)", self.name, ts, exc)
                    continue
                out.append(FetchedFile(filename=f"wayback_{kind}_{ts}.html", content=content, when=date.today()))
        content = self.http_get(WORKBOOK_URL)  # last, so the workbook's print of each day wins
        if content[:8] != bytes.fromhex("d0cf11e0a1b11ae1"):
            raise ValueError("WAPDA river-flow workbook: not an .xls file")
        sha = workbook_sha(content)
        if sha != WORKBOOK_SHA:  # WAPDA has updated the workbook in place: a new vintage
            log.warning("%s: workbook changed (sha256 %s, was %s) — rebuild its vintages (scripts/vintages_wapda.py)",
                        self.name, sha, WORKBOOK_SHA)
        out.append(FetchedFile(filename="GRAPH-DG-16-for-MAIL-2.xls", content=content, when=date.today()))
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        return to_records(file_prints(f)[1])
