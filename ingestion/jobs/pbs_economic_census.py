"""pbs_economic_census - Pakistan's first Economic Census (2023): establishments and workforce.

Source: PBS Economic Census results (pbs.gov.pk → Economic Statistics → Economic Census),
Excel workbooks under https://www.pbs.gov.pk/wp-content/uploads/2020/07/:
    EC_data_in_excel-1.xlsx              Pakistan and each province, by PSIC section
    {PROVINCE}-DISTRICT(S)-PSIC-WISE.xlsx  every district, by PSIC section
    {province}-district(s)-employment-category.xlsx
                                          every district, by PSIC section and size
                                          (fewer than 10 / 10 and more workers)

Section blocks look like

    ATTOCK                                   (district heading; may end in "DISTRICT")
    Sr. No. | Description | PSIC Codes | No of Establishments | Workforce
            |             |            | 68514                | 206271      (district total)
    1       | Agriculture, forestry and fishing | 1 to 3 | 7033 | 20211

Series (dims `province`, `district` [absent for province / national rows], `sector` — the
PSIC section name, "All sectors" for totals; size rows add `size`):
    regional.econ_census.establishments, regional.econ_census.workforce,
    regional.econ_census.establishments_by_size
Dated 2023-12-31. PBS blocks the VPS, so this runs from a workstation; the census is fixed.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record

BASE = "https://www.pbs.gov.pk/wp-content/uploads/2020/07"
OBS_DATE = date(2023, 12, 31)
SOURCE_URL = "https://www.pbs.gov.pk/pakistan-bureau-of-statistics-economic-statistics-production/"
FILES = {
    "national": "EC_data_in_excel-1.xlsx",
    "psic:Punjab": "PUNJAB-DISTRICTS-PSIC-wise-1.xlsx",
    "psic:Sindh": "SINDH-DISTRICTS-PSIC-WISE.xlsx",
    "psic:Khyber Pakhtunkhwa": "KPK-DISTRICTPSIC-WISE.xlsx",
    "psic:Balochistan": "BALOCHISTAN-DISTRICTPSIC-WISE.xlsx",
    "size:Punjab": "districts-punjabemployment-category.xlsx",
    "size:Sindh": "sindh-districts-employment-category-1.xlsx",
    "size:Khyber Pakhtunkhwa": "KPK-DISTRICTSEMPLOYMENT-CATEGORY.xlsx",
    "size:Balochistan": "Balochistanemployment-category.xlsx",
}
_PROVINCE_NAMES = {"pakistan": None, "punjab": "Punjab", "sindh": "Sindh", "kpk": "Khyber Pakhtunkhwa",
                   "khyber pakhtunkhwa": "Khyber Pakhtunkhwa", "balochistan": "Balochistan",
                   "islamabad": "Islamabad", "ict": "Islamabad"}

_C = dict(module="regional", frequency="census", source="PBS", source_url=SOURCE_URL, min_value=0, max_value=1e8)
_D = "by PSIC section (dimension `sector`), for Pakistan, each province and every district — Economic Census 2023"
CATALOG = [
    dict(id="regional.econ_census.establishments", name="Establishments (Economic Census 2023)", unit="count",
         description=f"Number of establishments {_D}.", **_C),
    dict(id="regional.econ_census.workforce", name="Workforce in establishments (Economic Census 2023)",
         unit="count", description=f"Persons working in establishments {_D}.", **_C),
    dict(id="regional.econ_census.establishments_by_size", name="Establishments by size (Economic Census 2023)",
         unit="count", description="Number of establishments with fewer than 10 / 10 or more workers (dimension "
                                   f"`size`), {_D[3:]}.", **_C),
]


def _title(s: str) -> str:
    s = re.sub(r"\s+DISTRICT$", "", s.strip(), flags=re.I)
    return " ".join(w.capitalize() for w in s.split())


def _sector(desc: str) -> str:
    s = re.sub(r"\s+", " ", str(desc)).strip()
    return s[:1].upper() + s[1:]


def _rows(content: bytes):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    for ws in wb.worksheets:
        yield ws.title, [list(r) for r in ws.iter_rows(values_only=True)]


def _int(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(round(v))
    if isinstance(v, str) and re.fullmatch(r"[\d,]+", v.strip()):
        return int(v.replace(",", ""))
    return None


def parse_national(content: bytes) -> list[Record]:
    """EC_data_in_excel Tables 1-6: one sheet per area (Pakistan, the provinces, ICT); the area
    name sits in the header row."""
    out: list[Record] = []
    for _, rows in _rows(content):
        if not rows or str(rows[0][0]).strip() != "Sr. No.":
            continue  # Tables 7-9 are unit-type / size cross-tabs, not the per-area section tables
        area = None
        for r in rows:
            cells = [c for c in r if c is not None]
            if area is None:
                for c in cells:
                    if isinstance(c, str) and c.strip().lower() in _PROVINCE_NAMES:
                        area = c.strip().lower()
                continue
            est, wf = (_int(r[3]) if len(r) > 3 else None), (_int(r[4]) if len(r) > 4 else None)
            if est is None:
                continue
            desc = r[1]
            sector = "All sectors" if isinstance(desc, str) and desc.strip().lower() == area else _sector(desc)
            dims = {"sector": sector}
            if _PROVINCE_NAMES[area]:
                dims = {"province": _PROVINCE_NAMES[area], **dims}
            out.append(Record("regional.econ_census.establishments", OBS_DATE, est, dims))
            if wf is not None:
                out.append(Record("regional.econ_census.workforce", OBS_DATE, wf, dims))
    if not out:
        raise ValueError("no Economic Census national/provincial rows parsed")
    return out


def parse_districts(content: bytes, province: str, size: bool = False) -> list[Record]:
    out: list[Record] = []
    for _, rows in _rows(content):
        district = None
        for r in rows:
            first = next((c for c in r if c is not None), None)
            nonnull = [c for c in r if c is not None]
            if len(nonnull) == 1 and isinstance(first, str) and first.isupper() and not first.startswith("TABLE"):
                district = _title(first)
                continue
            if district is None or len(r) < 5:
                continue
            a, b = _int(r[3]), _int(r[4])
            if a is None:
                continue
            if r[0] is None and r[1] is None:  # the block's total row
                sector = "All sectors"
            elif isinstance(r[1], str):
                sector = _sector(r[1])
            else:
                continue
            dims = {"province": province, "district": district, "sector": sector}
            if size:
                out.append(Record("regional.econ_census.establishments_by_size", OBS_DATE, a, {**dims, "size": "under 10"}))
                if b is not None:
                    out.append(Record("regional.econ_census.establishments_by_size", OBS_DATE, b,
                                      {**dims, "size": "10 and above"}))
            else:
                out.append(Record("regional.econ_census.establishments", OBS_DATE, a, dims))
                if b is not None:
                    out.append(Record("regional.econ_census.workforce", OBS_DATE, b, dims))
    if not out:
        raise ValueError(f"no district rows parsed for {province}")
    return out


class PbsEconomicCensusJob(IngestionJob):
    name = "pbs_economic_census"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        return [FetchedFile(filename=f"{key.replace(':', '__').replace(' ', '_')}.xlsx",
                            content=self.http_get(f"{BASE}/{fn}"), when=OBS_DATE) for key, fn in FILES.items()]

    def parse(self, f: FetchedFile) -> list[Record]:
        key = f.filename[:-5].replace("__", ":").replace("_", " ")
        if key == "national":
            return parse_national(f.content)
        kind, province = key.split(":", 1)
        return parse_districts(f.content, province, size=(kind == "size"))
