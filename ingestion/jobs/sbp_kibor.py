"""sbp_kibor — parse SBP's published KIBOR rates (same-day freshness).

Source: https://www.sbp.org.pk/ecodata/kibor/kibor.asp
The page renders a small table: a Tenor column and BID / Offer columns. Verified
against the live page (2026), which publishes the **3-M, 6-M and 12-M** benchmark
tenors (SBP's post-2026 site dropped the older 1W/2W/1M/9M rows and the historical
archive pages). We locate the table by its BID/Offer headers, so we tolerate the
surrounding page layout (which also carries T-Bill/PIB/Sukuk auction tables).

Scope note (matches the FX guidance in the roadmap): deep KIBOR **history** is
already ingested from EasyData (the `gp_bam_sirkibor_d.*` series). This scraper
exists only for same-day freshness ahead of EasyData's refresh, so `--backfill`
is a no-op — there is no live KIBOR archive to walk. bid/offer are carried in the
`side` dimension, consistent with the rest of the catalog.
"""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from ingestion.framework import FetchedFile, IngestionJob, Record

KIBOR_URL = "https://www.sbp.org.pk/ecodata/kibor/kibor.asp"

# Normalized tenor -> series id. 12-M is published as the 1-year benchmark.
_TENOR_TO_SERIES = {
    "3m": "kibor.3m",
    "6m": "kibor.6m",
    "1y": "kibor.1y",
}


def _normalize_tenor(text: str) -> str | None:
    """'3-M' -> '3m'; '12-M' -> '1y'; '1-Year' -> '1y'; '1 Week' -> '1w'."""
    t = text.strip().lower()
    m = re.search(r"(\d+)\s*[-\s]?\s*(week|month|year|w|m|y)\b", t)
    if not m:
        return None
    num, unit = m.group(1), m.group(2)
    unit_char = {"week": "w", "month": "m", "year": "y"}.get(unit, unit)
    tenor = f"{num}{unit_char}"
    return "1y" if tenor == "12m" else tenor


def _to_float(text: str) -> float | None:
    t = text.strip().replace(",", "").rstrip("%")
    if not t or t in {"-", "--", "N/A", "n/a"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def parse_kibor_html(html: str, fallback_date: date | None = None) -> tuple[date, list[Record]]:
    """Parse KIBOR HTML into (observation_date, records). Pure — no DB/network.

    The KIBOR table carries no date of its own (and other 'as on' dates on the
    page belong to the auction tables), so the observation date is the fetch date.
    """
    soup = BeautifulSoup(html, "lxml")
    obs_date = fallback_date or date.today()

    # Find the KIBOR table by its BID/Offer header (the auction tables below it
    # are headed 'Cut-off Yield', so they won't match).
    target = None
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True).lower()
        if "bid" in txt and "offer" in txt:
            target = table
            break
    if target is None:
        raise ValueError("KIBOR table (with BID/OFFER headers) not found")

    # Determine bid/offer column indexes from the header row.
    bid_idx = offer_idx = None
    for tr in target.find_all("tr"):
        cells = [c.get_text(" ", strip=True).lower() for c in tr.find_all(["td", "th"])]
        for i, c in enumerate(cells):
            if "bid" in c:
                bid_idx = i
            if "offer" in c:
                offer_idx = i
        if bid_idx is not None and offer_idx is not None:
            break
    if bid_idx is None or offer_idx is None:
        raise ValueError("could not locate BID/OFFER columns")

    records: list[Record] = []
    for tr in target.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        tenor = _normalize_tenor(cells[0])
        if tenor is None or tenor not in _TENOR_TO_SERIES:
            continue
        series_id = _TENOR_TO_SERIES[tenor]
        if bid_idx < len(cells):
            bid = _to_float(cells[bid_idx])
            if bid is not None:
                records.append(Record(series_id, obs_date, bid, {"side": "bid"}))
        if offer_idx < len(cells):
            offer = _to_float(cells[offer_idx])
            if offer is not None:
                records.append(Record(series_id, obs_date, offer, {"side": "offer"}))

    if not records:
        raise ValueError("no tenor rows parsed from KIBOR table")
    return obs_date, records


class SbpKiborJob(IngestionJob):
    name = "sbp_kibor"
    source = "SBP"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        content = self.http_get(KIBOR_URL)
        return [FetchedFile(filename="kibor.html", content=content, when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        _, records = parse_kibor_html(f.content.decode("utf-8", errors="replace"), f.when)
        return records
