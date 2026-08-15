"""pbs_cpi_monthly - PBS headline price indices and YoY inflation, monthly.

Source: https://www.pbs.gov.pk/wp-content/uploads/2020/07/indices_and_growth_rates_historical-1.pdf
(linked from /price-statistics/ as "Historical indices and growth rates").

The PDF holds TWO stacked sections, and they must not be conflated - the index
levels run ~100-300 while the rates run ~-2 to +40, so mixing them silently
corrupts the series:

    pages headed "Historical Indices"              -> index levels
    pages headed "Historical Inflation Rate (Y-oY)" -> YoY percent

Both use the same row shape (a section header only appears on its first pages,
so the section is carried forward):

    Year Month National UCPI RPI WPI      [then two old-base 2007-08 columns]
    2017    7    107.1   107.7 106.3 105.3

Fiscal-year average rows ("2016-17 104.8 ...") are skipped - they are not
monthly observations. Only the four Base 2015-16 columns are ingested; the
trailing old-base (2007-08) columns are ignored.
"""
from __future__ import annotations

import calendar
import io
import re
from datetime import date

import pdfplumber

from ingestion.framework import FetchedFile, IngestionJob, Record

CPI_URL = ("https://www.pbs.gov.pk/wp-content/uploads/2020/07/"
           "indices_and_growth_rates_historical-1.pdf")

# Year, Month, then the four Base 2015-16 measures (rates may be negative).
_ROW_RE = re.compile(
    r"^\s*(\d{4})\s+(\d{1,2})\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
    r"\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)"
)
_INDEX_HDR = re.compile(r"historical\s+indices", re.I)
_RATE_HDR = re.compile(r"inflation\s+rate", re.I)

# Column order in the PDF -> series id, per section.
_MEASURES = ["national", "urban", "rural", "wpi"]
_SERIES = {
    "index": {"national": "cpi.national", "urban": "cpi.urban",
              "rural": "cpi.rural", "wpi": "wpi"},
    "yoy": {"national": "cpi.national.yoy", "urban": "cpi.urban.yoy",
            "rural": "cpi.rural.yoy", "wpi": "wpi.yoy"},
}
ALL_SERIES = [sid for m in _SERIES.values() for sid in m.values()]


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_cpi_pdf(pdf_bytes: bytes) -> list[Record]:
    """Parse the historical indices/inflation PDF into records. Pure."""
    records: list[Record] = []
    section: str | None = None
    seen: set[tuple[str, date]] = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            # A header only appears on the first page(s) of each section, so the
            # section carries forward across continuation pages.
            if _RATE_HDR.search(text):
                section = "yoy"
            elif _INDEX_HDR.search(text):
                section = "index"
            if section is None:
                continue

            for line in text.splitlines():
                m = _ROW_RE.match(line)
                if not m:
                    continue
                year, month = int(m.group(1)), int(m.group(2))
                if not 1 <= month <= 12:
                    continue
                obs_date = _month_end(year, month)
                for i, measure in enumerate(_MEASURES):
                    sid = _SERIES[section][measure]
                    key = (sid, obs_date)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(Record(sid, obs_date, float(m.group(3 + i)), {}))

    if not records:
        raise ValueError("no CPI rows parsed from historical indices PDF")
    return records


class PbsCpiMonthlyJob(IngestionJob):
    name = "pbs_cpi_monthly"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # One PDF carries the full history, so incremental and backfill are the
        # same fetch; upserts keep it idempotent.
        content = self.http_get(CPI_URL)
        return [FetchedFile(filename="cpi_historical.pdf", content=content,
                            when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        return parse_cpi_pdf(f.content)
