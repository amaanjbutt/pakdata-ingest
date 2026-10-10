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
import logging
import re
from datetime import date

import pdfplumber

from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

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


# ---- monthly "Review on Price Indices" (newer months than the historical PDF) ----------
# PBS refreshes the historical PDF irregularly (it sat at June 2026 until at least
# October), but publishes a Monthly Review for every month, listed in the `cpidata1`
# array on /price-statistics/. A review carries the month's index levels (2 dp, the
# "General" row of Tables 1-3 and the WPI table) and Table 1.a with PBS's own 1-dp YoY
# figures - the precision the historical PDF uses, so a later historical refresh
# rewrites nothing.
REVIEWS_PAGE = "https://www.pbs.gov.pk/price-statistics/"
REVIEWS_TO_FETCH = 3
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_REVIEW_ENTRY = re.compile(r'month\s*:\s*"([A-Za-z]+)\s+(\d{4})"\s*,\s*review\s*:\s*"([^"]+\.pdf)"', re.I)
_REVIEW_MONTH = re.compile(r"Consumer Price Index for ([A-Za-z]+),?\s+(\d{4})", re.I)
_SECTION_TITLES = [  # (page title, measure) - the numbered sections, not the annexures
    (re.compile(r"^\s*I\.\s+National Consumer Price Index", re.M), "national"),
    (re.compile(r"^\s*II\.\s+Urban Consumer Price Index", re.M), "urban"),
    (re.compile(r"^\s*III\.\s+Rural Consumer Price Index", re.M), "rural"),
    (re.compile(r"^\s*V\.\s+Wholesale Price Index", re.M), "wpi"),
]
_GENERAL_ROW = re.compile(r"^General\s+100\.0+\s+(\d+\.\d+)\s", re.M)
_TABLE_1A_ROW = re.compile(r"^([A-Za-z]{3,9})-(\d{2})((?:\s+-?\d+(?:\.\d+)?){18})\s*$", re.M)


def review_links(html: str) -> list[tuple[date, str]]:
    """[(month_end, review_pdf_url)] from the /price-statistics/ page, newest first. Pure."""
    out: dict[date, str] = {}
    for mon, year, url in _REVIEW_ENTRY.findall(html):
        m = _MONTHS.get(mon.lower())
        if m:
            out.setdefault(_month_end(int(year), m), url)
    return sorted(out.items(), reverse=True)


def _half_up(v: float, dp: int = 1) -> float:
    from decimal import ROUND_HALF_UP, Decimal
    return float(Decimal(str(v)).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


def parse_review_pdf(pdf_bytes: bytes) -> list[Record]:
    """The review month's national/urban/rural CPI and WPI: index level (rounded to the
    historical PDF's 1 dp) + YoY from Table 1.a (PBS's own 1-dp figures). Pure."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    obs_date = None
    levels: dict[str, float] = {}
    yoy: dict[str, float] = {}
    for text in pages:
        for title, measure in _SECTION_TITLES:
            if measure not in levels and title.search(text):
                g = _GENERAL_ROW.search(text)
                if g:
                    levels[measure] = _half_up(float(g.group(1)))
                mm = _REVIEW_MONTH.search(text)
                if obs_date is None and mm and mm.group(1).lower() in _MONTHS:
                    obs_date = _month_end(int(mm.group(2)), _MONTHS[mm.group(1).lower()])
        if "Table 1.a" in text and not yoy:
            rows = _TABLE_1A_ROW.findall(text)
            if rows:
                vals = [float(x) for x in rows[-1][2].split()]
                # National, Urban, Rural (YoY, MoM) | food U/R | non-food U/R | SPI | WPI
                yoy = {"national": vals[0], "urban": vals[2], "rural": vals[4], "wpi": vals[16]}
                last = rows[-1]
                tbl_month = _MONTHS.get(last[0].lower()) or next(
                    (i for name, i in _MONTHS.items() if name.startswith(last[0].lower())), None)
                if obs_date and tbl_month and (obs_date.month != tbl_month or obs_date.year % 100 != int(last[1])):
                    yoy = {}  # Table 1.a's last row isn't the review month — don't guess
    if obs_date is None or not levels:
        raise ValueError("no index table parsed from the PBS monthly review")
    records = [Record(_SERIES["index"][m], obs_date, v, {}) for m, v in levels.items()]
    records += [Record(_SERIES["yoy"][m], obs_date, v, {}) for m, v in yoy.items()]
    return records


class PbsCpiMonthlyJob(IngestionJob):
    name = "pbs_cpi_monthly"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # One PDF carries the full history, so incremental and backfill are the
        # same fetch; upserts keep it idempotent.
        content = self.http_get(CPI_URL)
        files = [FetchedFile(filename="cpi_historical.pdf", content=content, when=date.today())]
        # Months the historical PDF hasn't caught up with come from the monthly
        # reviews. Strictly later months only, so the two sources never write the same
        # point (two writers flipping a value = a fake revision every run).
        self._historical_last = max(r.obs_date for r in parse_cpi_pdf(content))
        try:
            links = review_links(self.http_get(REVIEWS_PAGE).decode("utf-8", "replace"))
        except Exception as exc:  # the historical file alone still lands
            log.warning("pbs_cpi_monthly: review list unavailable (%s)", exc)
            links = []
        for month, url in links[:REVIEWS_TO_FETCH]:
            if month > self._historical_last:
                files.append(FetchedFile(filename=f"cpi_review_{month:%Y_%m}.pdf",
                                         content=self.http_get(url), when=date.today()))
        return files

    def parse(self, f: FetchedFile) -> list[Record]:
        if f.filename.startswith("cpi_review_"):
            last = getattr(self, "_historical_last", None)
            return [r for r in parse_review_pdf(f.content) if last is None or r.obs_date > last]
        return parse_cpi_pdf(f.content)
