"""sbp_auctions — SBP primary-market auction cut-off yields (T-Bill / PIB / Sukuk).

Source: https://www.sbp.org.pk/ecodata/kibor/kibor.asp — alongside KIBOR, this page
carries the latest auction cut-off results in labelled tables:
    MTBs               (T-Bills)      Tenor / Cut-off Yield
    Fixed - Rate PIB   (PIBs)         Tenor / Cut-off Yield
    GIS FRR / GIS VRR  (Ijara Sukuk)  Tenor / Cut-off Rental Rate/Price

We record each cut-off as a series (`auction.<type>.<tenor>.cutoff_yield`, or
`.cutoff_price` for GIS) AND a row in the dedicated `auctions` table (the one
sanctioned exception to the generic model). Offered/accepted amounts and
bid-to-cover are left NULL for now: the 2026 SBP site redesign dropped the
structured DMMD result tables that carried them, so they await a stable source.

NOTE (2026-09-12): kibor.asp's T-Bill/PIB auction tables went stale after the
redesign, so T-Bill & PIB cut-offs came only from EasyData via `derive_auctions`.
2026-11-12: the tables are current again and now print their own date — "(as on
Nov 11, 2026)" under the MTBs, "(as on Nov 04, 2026)" under the fixed-rate PIBs — while
EasyData's auction datasets lag weeks (last refreshed 2026-10-18, latest auction Oct 14).
So this job writes T-Bill and PIB cut-offs again, but ONLY from a table that prints its
date: the July-2026 page had an undated MTB table, and stamping it with the fetch day put
each auction on the following Thursday (Aug 20, Sep 3) next to EasyData's Wednesday point.
GIS tables never carry a date (see `drop_unchanged_undated`). `derive_auctions` fills the
history and never overwrites a point already stored (ON CONFLICT DO NOTHING), so a date
has one writer: kibor.asp's four-decimal cut-off where we saw it, EasyData's elsewhere.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from app import db
from ingestion.framework import FetchedFile, IngestionJob, Record

AUCTIONS_URL = "https://www.sbp.org.pk/ecodata/kibor/kibor.asp"

TBILL_TENORS = ["1m", "3m", "6m", "12m"]
PIB_TENORS = ["2y", "3y", "5y", "7y", "10y", "15y", "20y", "30y"]
GIS_TENORS = ["1y", "2y", "3y", "5y", "10y"]
_VALID = {"tbill": set(TBILL_TENORS), "pib": set(PIB_TENORS), "gis": set(GIS_TENORS)}

_DATE_PATTERNS = ["%d-%b-%y", "%d-%b-%Y", "%B %d, %Y", "%d %B %Y", "%b %d, %Y"]


def series_id(auction_type: str, tenor: str) -> str:
    metric = "cutoff_price" if auction_type == "gis" else "cutoff_yield"
    return f"rates.auction.{auction_type}.{tenor}.{metric}"


def _normalize_tenor(text: str) -> str | None:
    m = re.search(r"(\d+)\s*[-\s]?\s*([wmy])", text.strip().lower())
    if not m:
        return None
    return f"{m.group(1)}{m.group(2)}"


def _to_float(text: str) -> float | None:
    t = str(text).strip().replace(",", "").rstrip("%")
    if not t or "reject" in t.lower():
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _find_date(text: str) -> date | None:
    """The first date printed in `text` ("Nov 11, 2026", "04-Nov-26", ...), else None."""
    m = re.search(r"(\d{1,2}[-\s][A-Za-z]{3,9}[-\s]\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", text)
    if m:
        cand = re.sub(r"\s+", " ", m.group(1)).strip()
        for fmt in _DATE_PATTERNS:
            try:
                return datetime.strptime(cand, fmt).date()
            except ValueError:
                continue
    return None


def _classify(table, prev_text: str) -> str | None:
    header = " ".join(
        c.get_text(" ", strip=True).lower()
        for c in (table.find("tr").find_all(["td", "th"]) if table.find("tr") else [])
    )
    if "bid" in header and "offer" in header:
        return None  # KIBOR table, handled by sbp_kibor
    label = prev_text.lower()
    if "rental" in header or "gis" in label or "sukuk" in label:
        return "gis"
    if "mtb" in label or "t-bill" in label or "treasury" in label:
        return "tbill"
    if "pib" in label:
        return "pib"
    return None


def parse_auctions_html(html: str, fallback_date: date | None = None) -> list[Record]:
    """Parse auction cut-off tables into series records. Pure — no DB/network."""
    fallback_date = fallback_date or date.today()
    soup = BeautifulSoup(html, "lxml")
    records: list[Record] = []
    seen: set[tuple[str, str]] = set()

    for table in soup.find_all("table"):
        # Nearest preceding non-empty text gives the section label (MTBs/PIB/GIS).
        prev_text = ""
        node = table
        for _ in range(10):
            node = node.find_previous(string=True)
            if node and node.strip() and len(node.strip()) > 2:
                prev_text = node.strip()
                break
        atype = _classify(table, prev_text)
        if atype is None:
            continue
        printed = _find_date(table.get_text(" ", strip=True) + " " + prev_text)
        if printed is None and atype != "gis":
            continue  # an undated T-Bill/PIB table: its auction day is unknown (see module doc)
        adate = printed or fallback_date

        for tr in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            tenor = _normalize_tenor(cells[0])
            if tenor is None or tenor not in _VALID[atype]:
                continue
            val = _to_float(cells[1])
            if val is None:
                continue
            key = (atype, tenor)
            if key in seen:
                continue
            seen.add(key)
            records.append(Record(series_id(atype, tenor), adate, val, {}))
    if not records:
        raise ValueError("no auction cut-off rows parsed")
    return records


_SID_RE = re.compile(
    r"^(?:rates\.)?auction\.(tbill|pib|gis)\.([0-9]+[wmy])\.(cutoff_yield|cutoff_price)$"
)


def drop_unchanged_undated(
    recs: list[Record], fetch_day: date, last_values: dict[str, float]
) -> list[Record]:
    """kibor.asp's GIS tables carry no auction date, so those rows fall back to the
    fetch day. Re-emitting them every run stamped the SAME stale cut-off onto every
    calendar day (52 daily copies of two auctions, 2026-07/09). Like sbp_policy_rate,
    only emit an undated row when its value differs from the last stored one, so the
    series gets one point per actual auction (dated the day we first saw it). Rows
    with a real date parsed from the page are kept as-is (idempotent upsert)."""
    out: list[Record] = []
    for r in recs:
        if r.obs_date == fetch_day and last_values.get(r.series_id) == r.value:
            continue
        out.append(r)
    return out


class SbpAuctionsJob(IngestionJob):
    name = "sbp_auctions"
    source = "SBP"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        content = self.http_get(AUCTIONS_URL)
        return [FetchedFile(filename="auctions.html", content=content, when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        # Dated T-Bill/PIB tables + GIS (undated: emitted only when the value changes).
        # A page with no auction rows returns nothing (no_new_data) rather than raise —
        # kibor.asp dropping a table must not alert; a fetch error still fails the run.
        try:
            recs = parse_auctions_html(f.content.decode("utf-8", errors="replace"), f.when)
        except ValueError:
            return []
        return drop_unchanged_undated(recs, f.when, self._last_values([r.series_id for r in recs]))

    @staticmethod
    def _last_values(series_ids: list[str]) -> dict[str, float]:
        if not series_ids:
            return {}
        rows = db.query(
            """
            SELECT DISTINCT ON (series_id) series_id, value FROM observations
            WHERE series_id = ANY(%s) ORDER BY series_id, obs_date DESC
            """,
            (series_ids,),
        )
        return {r["series_id"]: float(r["value"]) for r in rows if r["value"] is not None}

    def upsert(self, validated: list[tuple[Record, bool]]) -> int:
        """Write the cut-off series to observations (via the base class) and mirror
        each into the dedicated auctions table."""
        n = super().upsert(validated)
        with db.connection() as conn:
            with conn.cursor() as cur:
                for r, _ in validated:
                    m = _SID_RE.match(r.series_id)
                    if not m:
                        continue
                    atype, tenor, _metric = m.groups()
                    cur.execute(
                        """
                        INSERT INTO auctions (auction_type, tenor, auction_date, cutoff_yield, source)
                        VALUES (%s, %s, %s, %s, 'SBP')
                        ON CONFLICT (auction_type, tenor, auction_date)
                        DO UPDATE SET cutoff_yield = EXCLUDED.cutoff_yield, revised_at = now()
                        WHERE auctions.cutoff_yield IS DISTINCT FROM EXCLUDED.cutoff_yield
                        """,
                        (atype, tenor, r.obs_date, r.value),
                    )
        return n
