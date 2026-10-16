"""pbs_lfs - Labour Force Survey: key labour-market indicators by province and sex.

Source: PBS Labour Force Survey annual report,
    https://www.pbs.gov.pk/wp-content/uploads/2020/07/LFS-2024-25-Annual-Report.pdf
"Key Labour Market Indicators by Sex and Provinces" tables:
    Table 3.3  2024-25, 19th ICLS (current international standard)
    Table 3.4  2020-21, 13th ICLS
    Table 3.5  2024-25, 13th ICLS (comparable with 2020-21)
Each block (Total / Both Sexes, Male, Female) lists Pakistan then KP, Punjab, Sindh, Balochistan:

    Provinces  Employed Unemployed LabourForce OutOfLabourForce WorkingAge  UR  EPR LFPR
    Total      77.2     5.9        83.1        96.5             179.6       7.1 43.0 46.3
    KP         11.2     1.2        12.5        17.8             30.3        9.6 37.2 41.2

Series (dims `province` [absent = Pakistan], `sex` [all|male|female], `standard`
[19th ICLS | 13th ICLS]); dated at the survey year's end (30 June):
    regional.lfs.unemployment_rate / employment_to_population / participation_rate   (percent)
    regional.lfs.employed / unemployed / labour_force / out_of_labour_force / working_age_population  (million)
PBS blocks the VPS: run from a workstation when a new LFS report is published.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record

REPORT_URL = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/LFS-2024-25-Annual-Report.pdf"
SOURCE_URL = "https://www.pbs.gov.pk/labour-force-statistics/"
TABLES = {"3.3": (date(2025, 6, 30), "19th ICLS"), "3.4": (date(2021, 6, 30), "13th ICLS"),
          "3.5": (date(2025, 6, 30), "13th ICLS")}
_COLS = ["employed", "unemployed", "labour_force", "out_of_labour_force", "working_age_population",
         "unemployment_rate", "employment_to_population", "participation_rate"]
_PROV = {"KP": "Khyber Pakhtunkhwa", "Punjab": "Punjab", "Sindh": "Sindh", "Balochistan": "Balochistan"}
_SEX = {"Total": "all", "Both Sexes": "all", "Male": "male", "Female": "female"}

_C = dict(module="regional", frequency="annual", source="PBS", source_url=SOURCE_URL)
_D = ("Labour Force Survey, Pakistan and provinces (dimension `province`; absent = Pakistan), by sex, under "
      "the 19th ICLS (current) or 13th ICLS (older, comparable) definition (dimension `standard`).")
_NAMES = {
    "unemployment_rate": ("Unemployment rate (LFS)", "percent"),
    "employment_to_population": ("Employment-to-population ratio (LFS)", "percent"),
    "participation_rate": ("Labour force participation rate (LFS)", "percent"),
    "employed": ("Employed persons (LFS)", "million"),
    "unemployed": ("Unemployed persons (LFS)", "million"),
    "labour_force": ("Labour force (LFS)", "million"),
    "out_of_labour_force": ("Working-age population outside the labour force (LFS)", "million"),
    "working_age_population": ("Working-age population (LFS)", "million"),
}
CATALOG = [dict(id=f"regional.lfs.{k}", name=n, unit=u, min_value=0, max_value=300 if u == "million" else 100,
                description=f"{n}. {_D}", **_C) for k, (n, u) in _NAMES.items()]

_ROW = re.compile(r"^(Total|Both Sexes|Male|Female|KP|Punjab|Sindh|Balochistan)\s+((?:[\d.]+\s+){7}[\d.]+)\s*$")


def parse_key_tables(text: str) -> list[Record]:
    """Report text → records for Tables 3.3 / 3.4 / 3.5. Pure."""
    out: list[Record] = []
    # split the text at each table title; keep the three key-indicator tables
    parts = re.split(r"(Table\s+3\.[345]\s+Key Labour Market Indicators[^\n]*)", text)
    for title, body in zip(parts[1::2], parts[2::2]):
        num = re.search(r"3\.[345]", title).group(0)
        if num not in TABLES or "...." in title:  # skip the list-of-tables entries
            continue
        obs, std = TABLES[num]
        sex = None
        for line in body.splitlines():
            m = _ROW.match(line.strip())
            if not m:
                if line.strip().startswith("Table "):
                    break
                continue
            label, vals = m.group(1), [float(v) for v in m.group(2).split()]
            if label in _SEX:
                sex = _SEX[label]
                dims = {"sex": sex, "standard": std}
            elif sex:
                dims = {"province": _PROV[label], "sex": sex, "standard": std}
            else:
                continue
            for col, v in zip(_COLS, vals):
                out.append(Record(f"regional.lfs.{col}", obs, v, dims))
    if not out:
        raise ValueError("no LFS key-indicator rows parsed")
    return out


class PbsLfsJob(IngestionJob):
    name = "pbs_lfs"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        return [FetchedFile(filename="lfs_2024_25_report.pdf", content=self.http_get(REPORT_URL),
                            when=date(2025, 6, 30))]

    def parse(self, f: FetchedFile) -> list[Record]:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            # the key tables sit in chapter 3 (pages ~45-60); read a generous window
            text = "\n".join(pg.extract_text() or "" for pg in pdf.pages[30:90])
        return parse_key_tables(text)
