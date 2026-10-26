"""pbs_cpi_groups - PBS CPI by COICOP major group, monthly (urban + rural).

Sources (linked from /price-statistics/):
    urban  CpI-Urban-Groupwise-Cumulative-Indices.pdf
    rural  RPI-Gropwise-Cumulative-Indices.pdf

Each page is one fiscal year (Jul-Jun) laid out wide - 12 monthly index columns
plus a fiscal average. Rows are a group>item hierarchy identified by an MG (major
group) code and a commodity code; the group total is the `00` commodity row.

**Base-year break:** in the urban file early pages use Base Year 2007-08, later
ones 2015-16, and those index levels are not comparable - so we ingest ONLY the
2015-16 base pages. (The rural file is 2015-16 throughout.) We take the group
totals (the 13 COICOP majors) and unpivot the 12 monthly columns into
observations dated to each month end, as `cpi.<area>.<group>`.

**Months after the cumulative files:** PBS refreshes the group-wise files only now and
then (they stood at Jun-2026 in Oct-2026), but every monthly CPI review carries the same
group indices in Annexure A (urban) / B (rural):

    Item and Specification   Weight    Sep 26  Aug 26  Sep 25  (% changes)
    General                  100.0000  300.11  296.24  272.53  + 1.31 + 10.12
    Food and non-alcoholic   30.4215   311.67  311.32  287.74  + 0.11 + 8.32

so months later than a file's last month come from the reviews (the review month's own
column only; strictly later months, so the two sources never write the same point).
"""
from __future__ import annotations

import calendar
import io
import logging
import re
from datetime import date

import pdfplumber

from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.jobs.pbs_cpi_monthly import REVIEWS_PAGE, review_links

log = logging.getLogger("pakdata.ingest")

# (area, url) - both PDFs share the same layout.
_SOURCES = [
    ("urban", "https://www.pbs.gov.pk/wp-content/uploads/2020/07/"
              "CpI-Urban-Groupwise-Cumulative-Indices.pdf"),
    ("rural", "https://www.pbs.gov.pk/wp-content/uploads/2020/07/"
              "RPI-Gropwise-Cumulative-Indices.pdf"),
]

_BASE_RE = re.compile(r"Base Year\s*=\s*(\d{4}-\d{2})", re.I)
_FY_RE = re.compile(r"\((\d{4})\s*-\s*(\d{4})\)")
CURRENT_BASE = "2015-16"

# COICOP major-group code -> series slug.
_GROUPS = {
    "00": "general", "01": "food", "02": "alcohol_tobacco", "03": "clothing",
    "04": "housing", "05": "furnishing", "06": "health", "07": "transport",
    "08": "communication", "09": "recreation", "10": "education",
    "11": "restaurants", "12": "misc",
}
ALL_SERIES = [f"cpi.{area}.{slug}" for area, _ in _SOURCES for slug in _GROUPS.values()]


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def _col_date(fy_start: int, col: int) -> date:
    """Column 0..11 of a Jul-Jun fiscal year -> month end. Jul(0)..Dec(5) sit in
    fy_start; Jan(6)..Jun(11) roll into the next calendar year."""
    if col < 6:
        return _month_end(fy_start, 7 + col)
    return _month_end(fy_start + 1, col - 5)


# A group-total line: SrNo, MG (2-digit group code), Cmdty '00', Description,
# then 12 monthly indices + a fiscal average. extract_tables() is unreliable on
# these dense pages, so we parse text lines. Two layouts occur across years:
#     "1 00 00 General ..."   (SrNo, MG, Cmdty separate)
#     "100 00 General ..."    (SrNo and MG glued in recent-year pages)
# so the code block before the Cmdty '00' is captured loosely and the MG is its
# last two digits.
_GROUP_LINE_RE = re.compile(r"^\s*(\d[\d\s]*?)\s+00\s+([A-Za-z].*)$")
_DECIMAL_RE = re.compile(r"-?\d+\.\d+")


def _mg_code(code_block: str) -> str:
    return re.sub(r"\s", "", code_block)[-2:]


def parse_cpi_groups_pdf(pdf_bytes: bytes, area: str = "urban") -> list[Record]:
    """Parse a groupwise CPI PDF into group-level records (2015-16 base only),
    tagged with the given area (urban|rural). Pure - no DB/network."""
    records: list[Record] = []
    seen: set[tuple[str, date]] = set()

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Each fiscal year spans several physical pages but the base-year and
        # fiscal-year headers appear only on its FIRST page - so both carry
        # forward across continuation pages (which hold the remaining groups).
        base = None
        fy_start = None
        for page in pdf.pages:
            text = page.extract_text() or ""
            bm = _BASE_RE.search(text)
            if bm:
                base = bm.group(1)
            fm = _FY_RE.search(text)
            if fm:
                fy_start = int(fm.group(1))
            if base != CURRENT_BASE or fy_start is None:
                continue

            for line in text.splitlines():
                m = _GROUP_LINE_RE.match(line)
                if not m:
                    continue
                mg = _mg_code(m.group(1))
                if mg not in _GROUPS:
                    continue
                nums = _DECIMAL_RE.findall(m.group(2))
                # need the 12 monthly values + a trailing fiscal average
                if len(nums) < 13:
                    continue
                monthly = [float(x) for x in nums[-13:-1]]  # drop the fiscal avg
                sid = f"cpi.{area}.{_GROUPS[mg]}"
                for col, val in enumerate(monthly):
                    obs_date = _col_date(fy_start, col)
                    key = (sid, obs_date)
                    if key in seen:
                        continue
                    seen.add(key)
                    records.append(Record(sid, obs_date, val, {}))

    if not records:
        raise ValueError("no group-level CPI rows parsed (2015-16 base)")
    return records


# Monthly review annexures: the group-total rows carry no item number (items start with
# one), then weight (4 dp) and three indices: review month, previous month, a year ago.
_ANNEX_AREA_RE = re.compile(r"Annexure\s+[AB]\s*:\s*CPI\s*\((Urban|Rural)\)", re.I)
_ANNEX_MONTH_RE = re.compile(r"%\s*Change in\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\s+over", re.I)
_REVIEW_GROUP_RE = re.compile(r"^([A-Za-z][A-Za-z.,&()\s-]*?)\s+\d{1,3}\.\d{4}\s+(\d+\.\d+)\s+\d+\.\d+\s+\d+\.\d+\s+[+-]")
# Group label (first words, lower case) -> series slug. Long labels wrap onto a second
# line ("Food and non-alcoholic / Beverages"), so only the first line is matched.
_REVIEW_GROUPS = [
    ("general", "general"), ("food and non", "food"), ("alcoholic beverages", "alcohol_tobacco"),
    ("clothing", "clothing"), ("housing", "housing"), ("furnishing", "furnishing"),
    ("health", "health"), ("transport", "transport"), ("communication", "communication"),
    ("recreation", "recreation"), ("education", "education"), ("restaurants", "restaurants"),
    ("misc", "misc"),
]
_MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


def parse_review_groups(pdf_bytes: bytes) -> list[Record]:
    """Group indices (urban + rural, 2015-16=100) for the review's own month from a PBS
    monthly CPI review's Annexures A and B. Pure. Raises when none are found (layout change)."""
    records: dict[tuple[str, date], Record] = {}
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            area_m, month_m = _ANNEX_AREA_RE.search(text), _ANNEX_MONTH_RE.search(text)
            if not area_m or not month_m or month_m.group(1).lower() not in _MONTH_ABBR:
                continue  # not an urban/rural annexure page (WPI annexure C, the narrative…)
            if "2015-16" not in text:
                raise ValueError("review annexure is not on base 2015-16 — check before loading")
            area = area_m.group(1).lower()
            obs_date = _month_end(int(month_m.group(2)), _MONTH_ABBR[month_m.group(1).lower()])
            for line in text.splitlines():
                m = _REVIEW_GROUP_RE.match(line.strip())
                if not m:
                    continue
                label = m.group(1).strip().lower()
                slug = next((s for prefix, s in _REVIEW_GROUPS if label.startswith(prefix)), None)
                if slug:
                    sid = f"cpi.{area}.{slug}"
                    records.setdefault((sid, obs_date), Record(sid, obs_date, float(m.group(2)), {}))
    if not records:
        raise ValueError("no CPI group rows in the review's annexures (layout change?)")
    return list(records.values())


class PbsCpiGroupsJob(IngestionJob):
    name = "pbs_cpi_groups"
    source = "PBS"
    # Reviews to consider beyond the cumulative files (they have lagged by a quarter).
    reviews_to_fetch = 12

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        self._file_last: dict[str, date] = {}
        for area, url in _SOURCES:
            try:
                content = self.http_get(url)
            except Exception:
                continue
            out.append(FetchedFile(filename=f"cpi_groups_{area}.pdf",
                                   content=content, when=date.today()))
            self._file_last[area] = max(r.obs_date for r in parse_cpi_groups_pdf(content, area=area))
        # Months the cumulative files haven't reached yet: from the monthly reviews.
        if self._file_last:
            oldest = min(self._file_last.values())
            try:
                links = review_links(self.http_get(REVIEWS_PAGE).decode("utf-8", "replace"))
            except Exception as exc:  # the cumulative files alone still land
                log.warning("pbs_cpi_groups: review list unavailable (%s)", exc)
                links = []
            # oldest first, so a later review never loses to an earlier one (dedupe_last)
            for month, url in sorted(links[: self.reviews_to_fetch]):
                if month > oldest:
                    out.append(FetchedFile(filename=f"cpi_review_{month:%Y_%m}.pdf",
                                           content=self.http_get(url), when=date.today()))
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        if f.filename.startswith("cpi_review_"):
            last = getattr(self, "_file_last", {})
            return [r for r in parse_review_groups(f.content)
                    if r.obs_date > last.get(r.series_id.split(".")[1], date.min)]
        area = "rural" if "rural" in f.filename else "urban"
        return parse_cpi_groups_pdf(f.content, area=area)
