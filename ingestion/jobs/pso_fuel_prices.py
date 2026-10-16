"""pso_fuel_prices - PSO retail fuel (POL) prices, every price change since May 2006.

Source: Pakistan State Oil's POL price archive,
    https://psopk.com/fuel-prices/pol/archives?page=N   (10 price notices per page, newest first)
Each notice is an accordion "Effective From: October 16, 2026" over a table of
product -> "Rs.390.23/Ltr". PSO labels every era's products by today's names
("PREMIER EURO 5" is its petrol / motor spirit, "HI-CETANE DIESEL EURO 5" its HSD).
A product PSO didn't sell prints "Rs.0/Ltr" or "--" and is skipped.

Each observation is the price effective FROM its date until the next observation.

The archive has a few clerical slips that we leave out rather than guess at:
  * a notice whose date breaks the archive's own (correct) newest-first order — e.g. a
    "January 01, 2006" placeholder between two 2011 notices, or "March 01, 2014" between
    2015 notices — is dropped (`misdated`);
  * a price more than 3x away from both neighbouring notices while they agree (e.g. one
    petrol price of 2222 between two of 64.27), or at either end of the history more than 3x
    from its only neighbour (jet fuel at 12.0 before 38.4 in 2006), is dropped (`spike`);
  * when one date has two notices, the first listed (PSO's latest entry) wins.
Every drop is listed in the run's result so it can be checked.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime

from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

URL = "https://psopk.com/fuel-prices/pol/archives"
SOURCE_URL = URL

# PSO product label -> (series id, name)
PRODUCTS = {
    "PREMIER EURO 5": ("energy.fuel.petrol", "Petrol (motor spirit) — PSO retail price"),
    "HI-CETANE DIESEL EURO 5": ("energy.fuel.diesel", "High-speed diesel — PSO retail price"),
    "SKO": ("energy.fuel.kerosene", "Kerosene oil (SKO) — PSO retail price"),
    "LDO": ("energy.fuel.ldo", "Light diesel oil (LDO) — PSO retail price"),
    "JP-1": ("energy.fuel.jp1", "Jet fuel (JP-1) — PSO price"),
    "E10 GASOLINE": ("energy.fuel.e10", "E10 gasoline — PSO retail price"),
}
CATALOG = [
    dict(id=sid, module="energy", name=name, unit="pkr_per_litre", frequency="irregular",
         source="PSO", source_url=SOURCE_URL, min_value=10, max_value=2000,
         description=f"{name}, rupees per litre, effective from each date until the next price "
                     "change. From Pakistan State Oil's POL price archive (May 2006 →).")
    for sid, name in PRODUCTS.values()
]

_NOTICE = re.compile(r"Effective From:\s*([A-Za-z]+ \d{1,2}, \d{4})(.*?)</table>", re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S)
_PRICE = re.compile(r"Rs\.?\s*([\d,]+(?:\.\d+)?)")
_LAST_PAGE = re.compile(r"[?&]page=(\d+)")


def _text(cell: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", cell)).strip()


def parse_page(text: str) -> list[tuple[date, dict[str, float]]]:
    """[(effective date, {product label: price})] in the page's (newest-first) order. Pure."""
    out = []
    for m in _NOTICE.finditer(text):
        d = datetime.strptime(m.group(1), "%B %d, %Y").date()
        prices: dict[str, float] = {}
        for row in _ROW.findall(m.group(2)):
            cells = [_text(c) for c in _CELL.findall(row)]
            if len(cells) != 2 or cells[0] not in PRODUCTS:
                continue
            pm = _PRICE.search(cells[1])
            if pm:
                v = float(pm.group(1).replace(",", ""))
                if v > 0:
                    prices.setdefault(cells[0], v)
        out.append((d, prices))
    return out


def last_page(text: str) -> int:
    return max([int(n) for n in _LAST_PAGE.findall(text)] or [1])


def backfill_edge_ok(v: float, neighbour: float) -> bool:
    """A price at either end of the history, with one neighbour to judge it by."""
    return neighbour / 3 <= v <= neighbour * 3


def clean_notices(notices: list[tuple[date, dict[str, float]]]) -> tuple[list[Record], list[str]]:
    """Archive order (newest first) -> records, dropping misdated notices, isolated price
    spikes and same-date duplicates (see module doc). Returns (records, dropped). Pure."""
    dropped: list[str] = []
    kept: list[tuple[date, dict[str, float]]] = []
    n = len(notices)
    for i, (d, p) in enumerate(notices):
        newer = kept[-1][0] if kept else None  # judged against what we kept, not a dropped slip
        older = notices[i + 1][0] if i + 1 < n else None
        # misdated: out of order with BOTH neighbours, while the neighbours are in order
        if newer and older and newer >= older and not (newer >= d >= older):
            dropped.append(f"misdated notice {d} (between {newer} and {older})")
            continue
        kept.append((d, p))
    records: list[Record] = []
    seen: set[tuple[str, date]] = set()
    for i, (d, p) in enumerate(kept):
        for label, v in p.items():
            sid = PRODUCTS[label][0]
            prev = next((kept[j][1][label] for j in range(i + 1, len(kept)) if label in kept[j][1]), None)
            nxt = next((kept[j][1][label] for j in range(i - 1, -1, -1) if label in kept[j][1]), None)
            if prev and nxt and abs(prev / nxt - 1) < 0.25 and (v > 3 * max(prev, nxt) or v < min(prev, nxt) / 3):
                dropped.append(f"spike {label} {v} on {d} (neighbours {prev}, {nxt})")
                continue
            only = prev if nxt is None else nxt if prev is None else None  # oldest / newest price
            if only and not backfill_edge_ok(v, only):
                dropped.append(f"spike {label} {v} on {d} (only neighbour {only})")
                continue
            if (sid, d) in seen:
                continue
            seen.add((sid, d))
            records.append(Record(sid, d, v, {}))
    return records, dropped


class PsoFuelPricesJob(IngestionJob):
    name = "pso_fuel_prices"
    source = "PSO"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # The cleaning rules need neighbouring notices, so pages are joined (newest first)
        # into one file: the first two pages (twenty notices) daily, the whole archive on
        # a backfill.
        first = self.http_get(f"{URL}?page=1")
        last = last_page(first.decode("utf-8", "replace")) if backfill else 2
        pages = [first] + [self.http_get(f"{URL}?page={n}") for n in range(2, last + 1)]
        return [FetchedFile(filename=f"pol_archive_p1-{last}.html", content=b"\n".join(pages),
                            when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        records, dropped = clean_notices(parse_page(f.content.decode("utf-8", "replace")))
        if not records:
            raise ValueError("no PSO price notices parsed")
        for d in dropped:
            log.warning("%s: %s", self.name, d)
        return records
