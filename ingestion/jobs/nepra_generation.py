"""nepra_generation - NEPRA plant-by-plant power generation, monthly, FY2017-18 →.

Source: NEPRA's State of Industry Report data pages — one Excel-to-HTML sheet per fiscal
year, "Details of Installed & Dependable Capacity (MW), Monthly Energy Generation (GWh) and
Plant Utilization (%)":
    https://nepra.org.pk/publications/State of Industry Reports/Detail of Generation/
        List of Companies Genenration wise {FY}.htm            (FY2017-18 … 2023-24)
    https://nepra.org.pk/publications/State of Industry Reports/SOI Data/
        List of Companies Genenration wise {FY} updated.htm    (FY2024-25)
Each wraps `…_files/sheet001.htm`, rows:

    S.No | Name of Companies | Technology | Fuel | Installed MW | Dependable MW |
    Jul % | Jul GWh | Aug % | Aug GWh | … | Jun % | Jun GWh | Sum % | Sum GWh

Series (dims `plant`; capacity rows also carry NEPRA's `technology` and `fuel`):
    energy.generation.plant      GWh per month
    energy.utilization.plant     % of dependable capacity, per month
    energy.capacity.installed    MW, dated at fiscal-year end (30 June)
    energy.capacity.dependable   MW, dated at fiscal-year end
    energy.generation.by_source  GWh per month, the plant figures summed by NEPRA's technology
                                 class (hydel, thermal, coal, nuclear, wind, solar, bagasse)

A plant renamed between years ("… Ltd." → "… (Pvt.) Limited") is matched on a normalised
key and shown under its latest name. NEPRA's own "Sum" column is not stored; where it
disagrees with the months (2 rows in 8 years, e.g. FFC Energy FY2021-22) the months are kept.
nepra.org.pk 403s the VPS, so this runs from a workstation/GitHub (`dq_fetch_parse.py job`)
once a year, when NEPRA adds the new fiscal year.
"""
from __future__ import annotations

import calendar
import html
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.plants_pk import canon_plant

BASE = "https://nepra.org.pk/publications/State%20of%20Industry%20Reports"
FIRST_FY = 2017  # FY2017-18
_PAGE = "List%20of%20Companies%20Genenration%20wise%20{fy}"
CANDIDATES = (f"{BASE}/SOI%20Data/{_PAGE}%20updated.htm", f"{BASE}/SOI%20Data/{_PAGE}.htm",
              f"{BASE}/Detail%20of%20Generation/{_PAGE}.htm")
SOURCE_URL = f"{BASE}.php"

SOURCES = {"HYDEL-(WAPDA)": "hydel", "HYDEL-(IPP)": "hydel", "THERMAL": "thermal", "THERMAL- COAL": "coal",
           "COAL": "coal", "NUCLEAR": "nuclear", "WIND": "wind", "SOLAR": "solar", "BIOGAS": "bagasse"}

_DESC = ("NEPRA State of Industry Report data: {what}, for every power plant NEPRA lists "
         "(dimension `plant`), FY2017-18 onward.")
CATALOG = [
    dict(id="energy.generation.plant", module="energy", name="Electricity generation by power plant",
         unit="gwh", frequency="monthly", source="NEPRA", source_url=SOURCE_URL, min_value=0, max_value=5000,
         description=_DESC.format(what="monthly energy generated (GWh)")),
    dict(id="energy.utilization.plant", module="energy", name="Power plant utilization",
         unit="percent", frequency="monthly", source="NEPRA", source_url=SOURCE_URL, min_value=0, max_value=200,
         description=_DESC.format(what="monthly plant utilization (% of dependable capacity)")),
    dict(id="energy.capacity.installed", module="energy", name="Installed capacity by power plant",
         unit="mw", frequency="annual", source="NEPRA", source_url=SOURCE_URL, min_value=0, max_value=10000,
         description=_DESC.format(what="installed capacity (MW) at fiscal-year end, with NEPRA's "
                                       "technology and fuel")),
    dict(id="energy.capacity.dependable", module="energy", name="Dependable capacity by power plant",
         unit="mw", frequency="annual", source="NEPRA", source_url=SOURCE_URL, min_value=0, max_value=10000,
         description=_DESC.format(what="dependable capacity (MW) at fiscal-year end, with NEPRA's "
                                       "technology and fuel")),
    dict(id="energy.generation.by_source", module="energy", name="Electricity generation by source",
         unit="gwh", frequency="monthly", source="NEPRA", source_url=SOURCE_URL, min_value=0, max_value=20000,
         description="Monthly generation (GWh) of the plants NEPRA lists, summed by NEPRA's technology class "
                     "(dimension `source`: hydel, thermal [oil, gas, RLNG], coal, nuclear, wind, solar, "
                     "bagasse). FY2017-18 onward."),
]

# The first sentence is what the series page and search results lead with; the declared
# `dimensions` are the headline filters (the series page and API examples default to them).
_LEADS = {'energy.generation.plant': 'Monthly electricity generation of every power plant in Pakistan.', 'energy.utilization.plant': 'Monthly utilization of every power plant in Pakistan.', 'energy.capacity.installed': 'Installed capacity of every power plant in Pakistan.', 'energy.capacity.dependable': 'Dependable capacity of every power plant in Pakistan.', 'energy.generation.by_source': "Pakistan's monthly electricity generation by source: hydel, thermal, coal, nuclear, wind, solar."}
_DIMS = {}
for _c in CATALOG:
    _c["description"] = f"{_LEADS[_c['id']]} {_c['description'].replace('`', '')}"
    if _c["id"] in _DIMS:
        _c["dimensions"] = _DIMS[_c["id"]]


_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_FY = re.compile(r"FY\s*(\d{4})-(\d{2})")
_DROP = {"pvt", "private", "ltd", "limited", "the", "company", "co", "plc"}


def _cell(x: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", x))).strip()


def _num(x: str) -> float | None:
    try:
        return float(x.replace(",", ""))
    except ValueError:
        return None


def plant_key(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(w for w in words if w not in _DROP)


def display_name(name: str) -> str:
    return re.sub(r"[.\s]+$", "", name).strip()


def sheet_url(frame_html: str, frame_url: str) -> str | None:
    m = re.search(r'href="([^"]*sheet0*1\.htm)"', frame_html, re.I)
    return frame_url.rsplit("/", 1)[0] + "/" + m.group(1) if m else None


def parse_sheet(text: str) -> tuple[int, list[dict]]:
    """(fiscal-year start, [{plant, technology, fuel, installed, dependable, months: [(date, pct, gwh)]}]).
    Pure."""
    m = _FY.search(_cell(text))
    if not m:
        raise ValueError("no 'FY yyyy-yy' header in NEPRA sheet")
    y0 = int(m.group(1))
    plants = []
    for row in _ROW.findall(text):
        c = [_cell(x) for x in _TD.findall(row)]
        if len(c) < 32 or not c[0].isdigit() or not c[1]:
            continue
        months = []
        for i in range(12):
            pct, gwh = _num(c[6 + 2 * i]), _num(c[7 + 2 * i])
            if gwh is None:
                continue
            y, mo = (y0, 7 + i) if i < 6 else (y0 + 1, i - 5)
            months.append((date(y, mo, calendar.monthrange(y, mo)[1]), pct, gwh))
        if not months:
            continue  # listed plant with no figures yet
        plants.append(dict(plant=display_name(c[1]), technology=c[2], fuel=c[3],
                           installed=_num(c[4]), dependable=_num(c[5]), months=months))
    if not plants:
        raise ValueError("no plant rows parsed from NEPRA sheet")
    return y0, plants


def to_records(sheets: list[tuple[int, list[dict]]]) -> list[Record]:
    """All fiscal years → records, plants matched across renames (latest name wins). Pure."""
    latest: dict[str, tuple[int, str]] = {}
    for y0, plants in sheets:
        for p in plants:
            k = plant_key(p["plant"])
            if k not in latest or y0 >= latest[k][0]:
                latest[k] = (y0, p["plant"])
    out: list[Record] = []
    by_source: dict[tuple[date, str], float] = {}
    for y0, plants in sheets:
        fy_end = date(y0 + 1, 6, 30)
        for p in plants:
            # registry name (+ unit for Guddu's blocks); renames across years map to one plant
            dims = canon_plant(latest[plant_key(p["plant"])][1])
            cap_dims = {**dims, "technology": p["technology"], "fuel": p["fuel"]}
            if p["installed"] is not None:
                out.append(Record("energy.capacity.installed", fy_end, p["installed"], cap_dims))
            if p["dependable"] is not None:
                out.append(Record("energy.capacity.dependable", fy_end, p["dependable"], cap_dims))
            src = SOURCES.get(p["technology"].strip().upper())
            for d, pct, gwh in p["months"]:
                out.append(Record("energy.generation.plant", d, gwh, dims))
                if pct is not None:
                    out.append(Record("energy.utilization.plant", d, pct, dims))
                if src:
                    by_source[(d, src)] = by_source.get((d, src), 0.0) + gwh
    out += [Record("energy.generation.by_source", d, round(v, 2), {"source": s}) for (d, s), v in by_source.items()]
    return out


SHEET_BREAK = "\n<!-- next NEPRA fiscal-year sheet -->\n"


class NepraGenerationJob(IngestionJob):
    name = "nepra_generation"
    source = "NEPRA"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # Plant renames are matched across years, so every fiscal year's sheet goes into one file.
        sheets = []
        last_fy = date.today().year if date.today().month >= 7 else date.today().year - 1
        for y0 in range(FIRST_FY, last_fy):
            fy = f"{y0}-{str(y0 + 1)[2:]}"
            for tpl in CANDIDATES:
                url = tpl.format(fy=fy)
                try:
                    frame = self.http_get(url).decode("cp1252", "replace")
                except Exception:  # noqa: BLE001 — try the next location
                    continue
                sheet = sheet_url(frame, url)
                if sheet:
                    sheets.append(self.http_get(sheet).decode("cp1252", "replace"))
                    break
        if not sheets:
            return []
        return [FetchedFile(filename="nepra_generation.htm", content=SHEET_BREAK.join(sheets).encode("utf-8"),
                            when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        return to_records([parse_sheet(t) for t in f.content.decode("utf-8").split(SHEET_BREAK)])
