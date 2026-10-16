"""pbs_hies - Household Integrated Economic Survey (HIES 2024-25, with 2018-19 for comparison).

Source: PBS HIES 2024-25 report,
    https://www.pbs.gov.pk/wp-content/uploads/2020/07/HIES-2024-25-Report-Final-2.pdf
Chapter 3 summary tables, each giving 2018-19 and 2024-25 side by side:
    3.2.C  average household size by province and region        AREA | 2018-19 | 2024-25
    3.3    average earners per household by province and region
    3.5.A  average monthly household income (Rs) by quintile     QUINTILE | URBAN RURAL TOTAL (2018-19)
    3.6.A  average monthly household consumption expenditure (Rs)          | URBAN RURAL TOTAL (2024-25) | % CHANGE
    3.6.B  per-capita monthly consumption expenditure (Rs)

Series (dated at the survey year's end, 30 June 2019 / 30 June 2025):
    regional.hies.household_size, regional.hies.earners_per_household
        dims `province` (Punjab, Sindh, Khyber Pakhtunkhwa, Balochistan) or `locality` (total|urban|rural)
    regional.hies.household_income, regional.hies.household_consumption,
    regional.hies.consumption_per_capita   (Rs per month)
        dims `locality` (total|urban|rural), `quintile` (1-5 by consumption, or all)
PBS blocks the VPS: run from a workstation when a new HIES report is published.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record

REPORT_URL = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/HIES-2024-25-Report-Final-2.pdf"
SOURCE_URL = "https://www.pbs.gov.pk/hies/"
Y1, Y2 = date(2019, 6, 30), date(2025, 6, 30)

_C = dict(module="regional", frequency="annual", source="PBS", source_url=SOURCE_URL)
_Q = "by quintile (dimension `quintile`, 1 = poorest; `all` = every household) and urban / rural (`locality`)"
CATALOG = [
    dict(id="regional.hies.household_size", name="Average household size (HIES)", unit="persons", min_value=0,
         max_value=20, description="Average persons per household, Pakistan (urban / rural, dimension `locality`) and "
                                   "by province (dimension `province`) — HIES 2018-19 and 2024-25.", **_C),
    dict(id="regional.hies.earners_per_household", name="Average earners per household (HIES)", unit="persons",
         min_value=0, max_value=10, description="Average income earners per household, Pakistan (urban / rural) "
                                                "and by province — HIES 2018-19 and 2024-25.", **_C),
    dict(id="regional.hies.household_income", name="Average monthly household income (HIES)", unit="pkr",
         min_value=0, max_value=10e6, description=f"Average monthly household income in rupees, {_Q} — HIES.", **_C),
    dict(id="regional.hies.household_consumption", name="Average monthly household consumption expenditure (HIES)",
         unit="pkr", min_value=0, max_value=10e6,
         description=f"Average monthly household consumption expenditure in rupees, {_Q} — HIES.", **_C),
    dict(id="regional.hies.consumption_per_capita", name="Monthly consumption expenditure per person (HIES)",
         unit="pkr", min_value=0, max_value=10e6,
         description=f"Average monthly consumption expenditure per person in rupees, {_Q} — HIES.", **_C),
]

_AREAS = {"Total": ("locality", "total"), "Urban": ("locality", "urban"), "Rural": ("locality", "rural"),
          "Punjab": ("province", "Punjab"), "Sindh": ("province", "Sindh"),
          "Khyber Pakhtunkhwa": ("province", "Khyber Pakhtunkhwa"), "Balochistan": ("province", "Balochistan")}
_AREA_ROW = re.compile(r"^(Total|Urban|Rural|Punjab|Sindh|Khyber Pakhtunkhwa|Balochistan)\s+([\d.]+)\s+([\d.]+)\s*$")
_Q_ROW = re.compile(r"^(1st|2nd|3rd|4th|5th|TOTAL)\s+((?:[\d.,]+\s+){5}[\d.,]+)(?:\s+[\d.]+)?\s*$")
_QUINTILE = {"1st": "1", "2nd": "2", "3rd": "3", "4th": "4", "5th": "5", "TOTAL": "all"}
_TABLES = [  # (title regex, series id, row kind)
    (r"TABLE\s+3\.2\.C\s+AVERAGE HOUSEHOLD SIZE", "regional.hies.household_size", "area"),
    (r"TABLE\s+3\.3\s+AVERAGE NUMBERS OF EARNERS", "regional.hies.earners_per_household", "area"),
    (r"TABLE:?\s+3\.5\.A\s+AVERAGE MONTHLY HOUSEHOLD INCOME", "regional.hies.household_income", "quintile"),
    (r"TABLE:?\s+3\.6\.A\s+AVERAGE MONTHLY HOUSEHOLD CONSUMPTION", "regional.hies.household_consumption", "quintile"),
    (r"TABLE\s+3\.6\.B\s+PER CAPITA MONTHLY CONSUMPTION", "regional.hies.consumption_per_capita", "quintile"),
]


def parse_summary_tables(text: str) -> list[Record]:
    """Report text → records for the chapter-3 summary tables. Pure."""
    out: list[Record] = []
    lines = text.splitlines()
    for title, sid, kind in _TABLES:
        start = next((i for i, ln in enumerate(lines) if re.search(title, ln)), None)
        if start is None:
            raise ValueError(f"HIES table not found: {title}")
        got = 0
        for ln in lines[start + 1: start + 30]:
            ln = ln.strip()
            if re.match(r"TABLE\b", ln):
                break  # the next table
            if kind == "area":
                m = _AREA_ROW.match(ln)
                if m:
                    k, v = _AREAS[m.group(1)]
                    out += [Record(sid, Y1, float(m.group(2)), {k: v}), Record(sid, Y2, float(m.group(3)), {k: v})]
                    got += 1
            else:
                m = _Q_ROW.match(ln)
                if m:
                    vals = [float(x.replace(",", "")) for x in m.group(2).split()]
                    q = _QUINTILE[m.group(1)]
                    for (obs, loc), v in zip([(Y1, "urban"), (Y1, "rural"), (Y1, "total"),
                                              (Y2, "urban"), (Y2, "rural"), (Y2, "total")], vals):
                        out.append(Record(sid, obs, v, {"locality": loc, "quintile": q}))
                    got += 1
                    if q == "all":
                        break
        if not got:
            raise ValueError(f"no rows under HIES table: {title}")
    return out


class PbsHiesJob(IngestionJob):
    name = "pbs_hies"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        return [FetchedFile(filename="hies_2024_25_report.pdf", content=self.http_get(REPORT_URL), when=Y2)]

    def parse(self, f: FetchedFile) -> list[Record]:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages[20:50])
        return parse_summary_tables(text)
