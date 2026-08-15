"""pbs_spi_weekly — parse the PBS weekly SPI (Sensitive Price Indicator) bulletin.

The authoritative per-city data is the SPI **Annexure** ("APPENDIX-A — CONSUMER
PRICES OF ESSENTIAL ITEMS"): a PDF matrix of 51 essential items x 17 cities,
each city reported as MIN / AVG / MAX. We ingest the **AVG** retail price per
item per city, and additionally derive a `national` series as the unweighted
mean of the city AVGs (there is no official per-item national column in the
Annexure — the derivation is documented in the catalog description).

Source layout (verified against real releases):
  - URL: https://www.pbs.gov.pk/wp-content/uploads/2020/07/Annex_DD.MM.YYYY.pdf
  - The date is the "week ended" date, always a **Thursday**; released Friday.
  - Missing/renamed weeks soft-404 with content-type text/html, so we accept a
    candidate only when it is served as application/pdf and begins with %PDF.
  - Each page carries a header row naming ~7 cities as "Lahore (05)" plus a
    MIN/AVG/MAX column triple per city; item rows follow. 17 cities span 3 pages.

Because PDF layouts drift, the parser matches **columns by city header text**
(not fixed positions) and **rows by item-description alias**, and reads the AVG
column as the offset immediately after each city's header cell.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta

import pdfplumber

from ingestion.commodities_config import CommodityItem, load_cities, load_items
from ingestion.framework import FetchedFile, IngestionJob, Record

ANNEX_URL = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Annex_{d:%d.%m.%Y}.pdf"
# How many weeks of history a --backfill walk reaches back over.
BACKFILL_WEEKS = 104
# How many recent weeks an incremental fetch probes (newest first).
INCREMENTAL_LOOKBACK_WEEKS = 3

_CITY_CODE_RE = re.compile(r"\(\s*\d{1,2}\s*\)")
_DATE_PATTERNS = ["%d-%m-%Y", "%d.%m.%Y", "%d-%b-%Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y"]


# ---- pure parsing ------------------------------------------------------------

def _parse_date(text: str) -> date | None:
    m = re.search(
        r"(\d{1,2}[-.\/]\d{1,2}[-.\/]\d{2,4}|\d{1,2}[-/\s][A-Za-z]{3,9}[-/\s]\d{2,4}"
        r"|[A-Za-z]+ \d{1,2}, \d{4})",
        text,
    )
    if not m:
        return None
    candidate = m.group(1)
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(text: str | None) -> float | None:
    if text is None:
        return None
    t = str(text).strip().replace(",", "")
    if not t or t in {"-", "--", "N/A", "n/a"}:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def _canon_city(cell: str) -> str:
    """'Islamabad (01)' -> 'islamabad' (strip the (dd) code)."""
    return _CITY_CODE_RE.sub("", cell).strip().lower()


def _match_item(label: str, items: list[CommodityItem]) -> CommodityItem | None:
    low = (label or "").lower()
    for it in items:
        if any(alias in low for alias in it.aliases):
            return it
    return None


def _city_avg_columns(header: list[str], cities: dict[str, str]) -> dict[int, str]:
    """Map the AVG column index -> canonical city key. A city header cell sits on
    its MIN column; AVG is the next column over."""
    out: dict[int, str] = {}
    for idx, cell in enumerate(header):
        if not cell or not _CITY_CODE_RE.search(cell):
            continue
        key = cities.get(_canon_city(cell))
        if key:
            out[idx + 1] = key  # AVG is the column after the city's MIN header
    return out


def _find_header(tables: list[list[list]], cities: dict[str, str]) -> list[str] | None:
    """The header row is the one naming >=2 cities (has (dd) codes)."""
    for t in tables:
        for row in t[:6]:
            named = sum(
                1 for c in row
                if c and _CITY_CODE_RE.search(c) and cities.get(_canon_city(c))
            )
            if named >= 2:
                return [c or "" for c in row]
    return None


def parse_spi_pdf(pdf_bytes: bytes, fallback_date: date | None = None) -> tuple[date, list[Record]]:
    """Parse an SPI Annexure PDF into (week_ended_date, records).

    Produces one AVG record per (item, city) plus a derived `national` mean.
    Pure — no DB/network. Raises ValueError if nothing parses (guards against a
    soft-404 HTML page slipping through as a PDF, or a layout we can't read)."""
    items = load_items()
    cities = load_cities()

    obs_date: date | None = None
    # (item_id) -> {city_key: avg_value}
    by_item: dict[str, dict[str, float]] = {}

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            if obs_date is None:
                for line in (page.extract_text() or "").splitlines():
                    if re.search(r"(prices on|week ended|w\.e\.f|for the week|dated)", line, re.I):
                        obs_date = _parse_date(line)
                        if obs_date:
                            break

            tables = page.extract_tables()
            if not tables:
                continue
            header = _find_header(tables, cities)
            if header is None:
                continue
            avg_cols = _city_avg_columns(header, cities)
            if not avg_cols:
                continue
            data = max(tables, key=len)  # the 51-row item table

            for row in data:
                if not row or not row[0] or not str(row[0]).strip().isdigit():
                    continue
                it = _match_item(row[1] if len(row) > 1 else "", items)
                if it is None:
                    continue
                dest = by_item.setdefault(it.id, {})
                for col_idx, city_key in avg_cols.items():
                    if col_idx >= len(row):
                        continue
                    val = _to_float(row[col_idx])
                    if val is None or val <= 0:  # 0.00 == not sold / not available
                        continue
                    dest.setdefault(city_key, val)  # first page wins if duplicated

    if obs_date is None:
        obs_date = fallback_date or date.today()

    records: list[Record] = []
    for item_id, city_vals in by_item.items():
        for city_key, val in city_vals.items():
            records.append(Record(item_id, obs_date, val, {"city": city_key}))
        if city_vals:  # derived unweighted national average across reporting cities
            national = round(sum(city_vals.values()) / len(city_vals), 2)
            records.append(Record(item_id, obs_date, national, {"city": "national"}))

    if not records:
        raise ValueError("no commodity rows parsed from SPI Annexure PDF")
    return obs_date, records


# ---- job ---------------------------------------------------------------------

def _recent_thursdays(n: int, ref: date | None = None) -> list[date]:
    """The n most recent Thursdays (SPI week-ended day), newest first."""
    ref = ref or date.today()
    # Monday=0 ... Thursday=3
    offset = (ref.weekday() - 3) % 7
    last_thu = ref - timedelta(days=offset)
    return [last_thu - timedelta(weeks=i) for i in range(n)]


class PbsSpiWeeklyJob(IngestionJob):
    name = "pbs_spi_weekly"
    source = "PBS"

    def _get_pdf(self, d: date) -> bytes | None:
        """Fetch the Annexure for a week-ended date, or None if that week's file
        is absent (served as an HTML soft-404 rather than a PDF)."""
        import httpx

        from app.config import settings

        url = ANNEX_URL.format(d=d)
        try:
            resp = httpx.get(
                url, headers={"User-Agent": settings.user_agent},
                timeout=45, follow_redirects=True,
            )
        except httpx.HTTPError:
            return None
        ctype = resp.headers.get("content-type", "")
        if resp.status_code == 200 and "pdf" in ctype and resp.content[:4] == b"%PDF":
            return resp.content
        return None

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        weeks = BACKFILL_WEEKS if backfill else INCREMENTAL_LOOKBACK_WEEKS
        out: list[FetchedFile] = []
        for d in _recent_thursdays(weeks):
            content = self._get_pdf(d)
            if content is None:
                continue
            out.append(FetchedFile(filename=f"Annex_{d:%d.%m.%Y}.pdf", content=content, when=d))
            if not backfill:
                break  # incremental only needs the newest available week
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        _, records = parse_spi_pdf(f.content, f.when)
        return records
