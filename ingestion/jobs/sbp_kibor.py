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

The same page also carries SBP's daily market snapshot, parsed here with no extra
fetch (2026-10-04): the USD/PKR **M2M revaluation rate** and **weighted-average
interbank bid/offer**, the **weighted-average overnight repo rate**, and the weekly
**liquid FX reserves** (SBP / banks / total). Each block is dated by its own "As on"
date and is optional — a layout change in one never costs us KIBOR.
"""
from __future__ import annotations

import re
from datetime import date, datetime

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


_DATE = r"(\d{1,2})\s*-\s*([A-Za-z]{3,9})\s*-\s*(\d{2,4})"
_NUM = r"([\d,]+(?:\.\d+)?)"


def _page_date(day: str, month: str, year: str) -> date | None:
    """('22', 'Jul', '26') / ('10', 'July', '2026') -> date."""
    y = int(year) + (2000 if len(year) == 2 else 0)
    for fmt in ("%b", "%B"):
        try:
            return datetime.strptime(f"{int(day)} {month[:3] if fmt == '%b' else month} {y}", f"%d {fmt} %Y").date()
        except ValueError:
            continue
    return None


def _page_text(soup: BeautifulSoup) -> str:
    # One space-collapsed string; curly apostrophes vary (SBP’s / mojibake).
    return " ".join(soup.get_text(" ").split())


def parse_market_snapshot(html: str) -> list[Record]:
    """SBP's daily market snapshot on kibor.asp -> records, each dated by its own
    "As on" date. Pure. Missing blocks are skipped (never raises)."""
    text = _page_text(BeautifulSoup(html, "lxml"))
    out: list[Record] = []

    m = re.search(
        r"USD\s*/\s*PKR\s+Rates\s+As\s+on\s+" + _DATE
        + r"\s+M2M\s+Revaluation\s+Rate\s+" + _NUM
        + r"\s+Weighted\s+Average\s+Rate\s+BID\s+" + _NUM + r"\s+Offer\s+" + _NUM,
        text, re.I,
    )
    if m and (d := _page_date(*m.group(1, 2, 3))):
        m2m, bid, offer = (_to_float(m.group(i)) for i in (4, 5, 6))
        if m2m is not None:
            out.append(Record("fx.rate.m2m.usd", d, m2m, {}))
        if bid is not None:
            out.append(Record("fx.rate.interbank.usd", d, bid, {"side": "bid"}))
        if offer is not None:
            out.append(Record("fx.rate.interbank.usd", d, offer, {"side": "offer"}))

    m = re.search(
        r"Liquid\s+Foreign\s+Exchange\s+Reserves\s*\(USD\s+million\)\s+As\s+on\s+" + _DATE
        + r"\s+SBP\S*\s+Reserves\s+" + _NUM + r"\s+Bank\S*\s+Reserves\s+" + _NUM
        + r"\s+Total\s+Reserves\s+" + _NUM,
        text, re.I,
    )
    if m and (d := _page_date(*m.group(1, 2, 3))):
        for sid, i in (("reserves.liquid.sbp", 4), ("reserves.liquid.banks", 5), ("reserves.liquid.total", 6)):
            v = _to_float(m.group(i))
            if v is not None:
                out.append(Record(sid, d, v, {}))

    m = re.search(
        r"overnight\s+repo\s+rate\s+As\s+on\s+" + _DATE + r"\s+" + _NUM + r"\s*%",
        text, re.I,
    )
    if m and (d := _page_date(*m.group(1, 2, 3))):
        v = _to_float(m.group(4))
        if v is not None:
            out.append(Record("rates.repo.overnight", d, v, {}))
    return out


def parse_kibor_html(html: str, fallback_date: date | None = None) -> tuple[date, list[Record]]:
    """Parse KIBOR HTML into (observation_date, records). Pure — no DB/network.

    Dated by the page's own "KIBOR As on <date>" line, so a weekend or holiday run
    doesn't stamp Friday's fixing onto Saturday; falls back to the fetch date when
    that line is missing.
    """
    soup = BeautifulSoup(html, "lxml")
    obs_date = fallback_date or date.today()
    m = re.search(r"KIBOR\s+As\s+on\s+" + _DATE, _page_text(soup), re.I)
    if m and (d := _page_date(*m.group(1, 2, 3))):
        obs_date = d

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
        html = f.content.decode("utf-8", errors="replace")
        _, records = parse_kibor_html(html, f.when)
        return records + parse_market_snapshot(html)
