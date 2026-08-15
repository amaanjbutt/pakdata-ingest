"""sbp_sme_finance - SBP Quarterly SME Finance Review (Phase 6).

Source: the "Quarterly SME Finance Review" data tables PDF, on the redesigned
SBP site under
  /our-operations/publications/quarterly-sme-finance-review/389
whose landing links to one child page per quarter, each carrying the PDF under
/assets/document/publications/sme_qFR_<Mon>_<Year>.pdf.

The recent PDF is a single page of five clean grid tables (parsed via
pdfplumber extract_tables), each with three quarter columns:
  1. SME financing profile (outstanding, private-sector financing, share, NPL
     ratio, borrowers),
  2. facility-wise composition, 3. sector-wise, 4. bank-cluster share,
  5. Islamic SME finance.

Each PDF carries three quarters, so parsing the latest each quarter accumulates
history; `--backfill` walks older child pages (off-format older PDFs are skipped
by the framework's per-file resilience). SME finance is genuinely absent from
EasyData.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from ingestion.framework import Record
from ingestion.pdf_report import PdfReportJob

LANDING = "https://www.sbp.org.pk/our-operations/publications/quarterly-sme-finance-review/389"
_CHILD = r"quarterly-sme-finance-review/389/[a-z0-9-]+"
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_name) if m})


class Series:
    __slots__ = ("id", "name", "unit", "min_value", "max_value")

    def __init__(self, sid, name, unit, min_value, max_value):
        self.id, self.name, self.unit = sid, name, unit
        self.min_value, self.max_value = min_value, max_value


_BN = ("pkr_bn", 0, 100000)   # Rs billion amounts
_PCT = ("percent", -5, 100)   # ratios/shares
_CNT = ("count", 0, 100000000)

# section number (Table No N) -> {row-label prefix: Series}
_SECTIONS: dict[int, dict[str, Series]] = {
    1: {
        "Outstanding SME Financing": Series("sme.outstanding", "Outstanding SME Financing", *_BN),
        "Domestic Private Sector": Series("sme.private_sector_financing", "Domestic Private Sector Financing", *_BN),
        "SME Fin as": Series("sme.share_pvt_sector", "SME Finance as % of Domestic Private Sector Financing", *_PCT),
        "SME NPLs Ratio": Series("sme.npl_ratio", "SME NPLs Ratio", *_PCT),
        "No. of SME Borrowers": Series("sme.borrowers", "Number of SME Borrowers", *_CNT),
    },
    2: {
        "Fixed Investment": Series("sme.facility.fixed_investment", "SME Financing - Fixed Investment", *_BN),
        "Working Capital": Series("sme.facility.working_capital", "SME Financing - Working Capital", *_BN),
        "Trade Finance": Series("sme.facility.trade_finance", "SME Financing - Trade Finance", *_BN),
    },
    3: {
        "Trading SMEs": Series("sme.sector.trading", "SME Financing - Trading", *_BN),
        "Manufacturing SMEs": Series("sme.sector.manufacturing", "SME Financing - Manufacturing", *_BN),
        "Services SMEs": Series("sme.sector.services", "SME Financing - Services", *_BN),
    },
    4: {
        "Domestic Private Banks": Series("sme.bank_share.private", "SME Financing - Domestic Private Banks", *_BN),
        "Public Sector Commercial": Series("sme.bank_share.public", "SME Financing - Public Sector Commercial Banks", *_BN),
        "Islamic Banks": Series("sme.bank_share.islamic", "SME Financing - Islamic Banks", *_BN),
        "Specialized Banks": Series("sme.bank_share.specialized", "SME Financing - Specialized Banks & Others", *_BN),
        "DFIs": Series("sme.bank_share.dfis", "SME Financing - DFIs", *_BN),
    },
    5: {
        "Islamic Banking Divisions": Series("sme.islamic.divisions", "Islamic SME Financing - Islamic Banking Divisions", *_BN),
        "Islamic Banks": Series("sme.islamic.banks", "Islamic SME Financing - Islamic Banks", *_BN),
        "Total": Series("sme.islamic.total", "Islamic SME Financing - Total", *_BN),
    },
}

ALL_SERIES: list[Series] = [s for sec in _SECTIONS.values() for s in sec.values()]

_MON_RE = re.compile(r"^([A-Za-z]{3})-(\d{2})$")
_TABLE_RE = re.compile(r"Table\s*No\s*(\d)")


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _clean(cell: str | None) -> float | None:
    """A data cell -> float. Handles thousands commas, a stray intra-number space
    from PDF extraction ('4 20.47' -> 420.47), and a trailing percent sign."""
    if cell is None:
        return None
    s = cell.strip().replace(",", "").replace(" ", "").rstrip("%")
    if not s or not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return float(s)


def _header_dates(rows: list[list[str | None]]) -> list[date]:
    for row in rows:
        cells = [(c or "").strip() for c in row]
        months = [_MON_RE.match(c) for c in cells]
        got = [m for m in months if m]
        if len(got) >= 3:
            return [_month_end(2000 + int(m.group(2)), _MONTHS[m.group(1).lower()])
                    for m in got]
    return []


def parse_sme_finance(pages) -> list[Record]:
    """Parse the SME Financing Data Tables PDF. Pure - no net."""
    rows: list[list[str | None]] = []
    for pg in pages:
        for tbl in pg.extract_tables():
            rows.extend(tbl)

    dates = _header_dates(rows)
    if len(dates) < 3:
        raise ValueError("no quarter-column header found in SME PDF")
    dates = dates[:3]

    records: list[Record] = []
    section = 0
    for row in rows:
        cells = [(c or "").strip() for c in row]
        joined = " ".join(cells)
        tm = _TABLE_RE.search(joined)
        if tm:
            section = int(tm.group(1))
            continue
        label = cells[0]
        if not label:
            continue
        nums = [v for v in (_clean(c) for c in cells[1:]) if v is not None]
        if len(nums) < 3:
            continue
        for prefix, s in _SECTIONS.get(section, {}).items():
            if label.startswith(prefix):
                for d, v in zip(dates, nums[:3]):
                    records.append(Record(s.id, d, v, {}))
                break

    if not records:
        raise ValueError("SME tables found but no rows parsed")
    return records


class SbpSmeFinanceJob(PdfReportJob):
    name = "sbp_sme_finance"
    source = "SBP"

    def _child_pages(self) -> list[tuple[tuple[int, int], str]]:
        """((year, month), url) for each quarterly child page, newest first."""
        out: list[tuple[tuple[int, int], str]] = []
        for url in self._links(LANDING, _CHILD):
            m = re.search(r"-([a-z]+)-(\d{4})$", url, re.I)
            if m and m.group(1).lower() in _MONTHS:
                out.append(((int(m.group(2)), _MONTHS[m.group(1).lower()]), url))
        return sorted(set(out), reverse=True)

    def report_urls(self, backfill: bool) -> list[tuple[str, date]]:
        pages = self._child_pages()
        if not backfill:
            pages = pages[:2]            # newest couple of quarters
        else:
            pages = pages[:24]           # ~6 years; older PDFs pre-date this format
        out: list[tuple[str, date]] = []
        for (yr, mo), page_url in pages:
            for pdf in self._links(page_url, r"\.pdf$"):
                if re.search(r"(sme|qFR|QSMEF)", pdf, re.I):
                    out.append((pdf, _month_end(yr, mo)))
        return out

    def parse_pdf(self, pages, when: date) -> list[Record]:
        return parse_sme_finance(pages)
