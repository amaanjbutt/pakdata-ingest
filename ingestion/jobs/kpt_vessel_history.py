"""kpt_vessel_history - every ship call at Karachi Port: when each ship arrived and sailed, and what it came
to do (discharge, load or both, and which cargo).

Source: Karachi Port Trust, "Vessel History", https://kpt.gov.pk/pages/87/vessel-history?dtStart=YYYY-MM-DD
&dtEnd=YYYY-MM-DD (other date formats redirect to an error page). Without dates the page shows the last three
months; with them, every ship that arrived in the range — but KPT's system starts on 1 May 2026, so that is
as far back as it goes (2026-11-14: 1,273 calls, 1 May → 14 Nov). One table per KPT ship category
("Container Ship", "Tanker", "Rice", "Clinkers", "Car Carrier" …), each row: category, vessel name, arrival
date-time, departure date-time (blank while the ship is in port) and purpose ("Disc./Load Containers",
"Load Rice", "Discharge Crude Oil"; blank on a few tankers). The server takes ~45 s to answer whatever the
range, so the job asks once for its whole window.

Stored: `port_vessel_calls`, port 'KPT', section 'calls' — one row per call, dated by its arrival day
(`statement_date` = `arrived_on`), the arrival time in details['arrived_at'], the sailing time in
`departs_at`, KPT's category in details['kpt_type'], the purpose in details['work'] (its cargo part in
`commodity_raw`, readable in `commodity`). Each run re-reads every arrival day of its window and replaces
that day's calls (a departure filled in later updates the row; a call KPT drops is removed); days before
the window keep what earlier runs stored, so the history grows past KPT's own. A call listed twice with
one arrival (a ship that shifted berth) gets seq 0, 1 by departure.

Where it runs: GitHub Actions (`ports.yml`, with kpt_shipping). kpt.gov.pk answers GitHub's runners since
November 2026 (6/6 on 2026-11-14; it reset them on 2026-10-31) and still times out from the VPS.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, timedelta

from ingestion import http_client, ports_pk
from ingestion.framework import FetchedFile, IngestionJob
from ingestion.jobs.kpt_shipping import _OP, PORT
from ingestion.port_calls import PortCallsMixin, VesselRow

log = logging.getLogger("pakdata.ingest")

PAGE_URL = "https://kpt.gov.pk/pages/87/vessel-history"
FIRST_DAY = date(2026, 5, 1)   # KPT's vessel history starts here (nothing earlier for any range)
WINDOW_DAYS = 120              # a daily run re-reads the last four months; --backfill everything since FIRST_DAY
SECTION = "calls"
_RANGE = re.compile(r"From\s*:\s*([A-Za-z]{3})-(\d{1,2})-(\d{4})\s+to\s+([A-Za-z]{3})-(\d{1,2})-(\d{4})")
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"), 1)}
_WHEN = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]m)?$", re.I)


def _cell(c: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()


def when(text: str) -> datetime | None:
    """'14/08/2026 2:00:00 pm' -> datetime(2026, 8, 14, 14, 0) (day first, 12-hour clock); None if blank. Pure."""
    m = _WHEN.match(text.strip())
    if not m:
        if text.strip():
            raise ValueError(f"unreadable date-time {text!r}")
        return None
    d, mo, y, h, mi, _s, ap = m.groups()
    h = int(h)
    if ap:
        h = h % 12 + (12 if ap.lower() == "pm" else 0)
    return datetime(int(y), int(mo), int(d), h, int(mi))


def page_range(page: str) -> tuple[date, date] | None:
    """The 'From : Aug-14-2026 to Nov-14-2026' line the page prints above its tables. Pure."""
    m = _RANGE.search(page)
    if not m:
        return None
    a = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    b = date(int(m.group(6)), _MONTHS[m.group(4).lower()], int(m.group(5)))
    return a, b


def calls(page: str) -> list[dict]:
    """Every row of the page's tables: {kpt_type, vessel, arrived, departed, purpose}. Pure."""
    i = page.find('class="ContainerGrid"')
    if i < 0:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", page[i:], re.S | re.I):
        td = [_cell(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(td) != 5:
            continue
        kpt_type, vessel, arrived, departed, purpose = td
        out.append({"kpt_type": kpt_type, "vessel": vessel, "arrived": when(arrived), "departed": when(departed),
                    "purpose": purpose})
    return out


def vessel_name(text: str) -> str:
    """'M.T.SARGODHA' -> 'SARGODHA', 'PEARL.' -> 'PEARL' (as printed otherwise). Pure."""
    return re.sub(r"^M\.?\s*[TV](?:\.\s*|\s+)(?=\S)", "", text.strip()).strip(" .")


def _vessel_type(kpt_type: str, work: str) -> str | None:
    """KPT's own category where it names the ship's kind (a "Tanker" discharging palm oil is a tanker), else
    the same reading of the cargo as the berth rows'. Pure."""
    k = kpt_type.lower()
    if "tanker" in k:
        return "tanker"
    if "container" in k:
        return "container"
    return ports_pk.kpt_vessel_type(work) or ("bulk_or_general" if k else None)


def to_rows(items: list[dict], source: str) -> list[VesselRow]:
    """Calls -> vessel rows (section 'calls', dated by arrival); seq numbers a call listed twice with one
    arrival, in departure order. Pure."""
    rows: list[VesselRow] = []
    for it in items:
        if it["arrived"] is None:
            raise ValueError(f"a call without an arrival time: {it['vessel']!r}")
        work = it["purpose"]
        op = _OP.match(work)
        what = work[op.end():].strip() if op else work
        opword = op.group(0).lower() if op else ""
        direction = "" if "/" in opword or not opword else ("export" if opword.startswith("load") else "import")
        name, _unit = ports_pk.commodity(what) if what else (None, None)
        details = {"arrived_at": it["arrived"].isoformat(timespec="minutes"), "kpt_type": it["kpt_type"]}
        if work:
            details["work"] = work
        rows.append(VesselRow(port=PORT, statement_date=it["arrived"].date(), section=SECTION,
                              vessel=vessel_name(it["vessel"]), direction=direction, commodity_raw=what,
                              commodity=name, vessel_type=_vessel_type(it["kpt_type"], work),
                              arrived_on=it["arrived"].date(), departs_at=it["departed"], details=details,
                              source_file=source))
    # stable numbering: a key listed twice is told apart by its departure (still in port last)
    rows.sort(key=lambda r: (r.key(), r.departs_at is None, r.departs_at or datetime.min))
    seen: dict[tuple, int] = {}
    for r in rows:
        k = r.key()
        r.seq = seen.get(k, 0)
        seen[k] = r.seq + 1
    return rows


class KptVesselHistoryJob(PortCallsMixin, IngestionJob):
    name = "kpt_vessel_history"
    source = "KPT"
    replace_sections = (SECTION,)

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        today = date.today()
        start = FIRST_DAY if backfill else max(FIRST_DAY, today - timedelta(days=WINDOW_DAYS))
        url = f"{PAGE_URL}?dtStart={start.isoformat()}&dtEnd={(today + timedelta(days=1)).isoformat()}"
        content = http_client.get(url, timeout=240).content
        return [FetchedFile(filename=f"kpt_vessel_history_{start.isoformat()}.html", content=content, when=today)]

    def parse(self, f: FetchedFile) -> list:
        page = f.content.decode("utf-8", "replace")
        rng = page_range(page)
        if rng is None:
            raise ValueError("no vessel history on the page (no date range: an error page?)")
        items = calls(page)
        if not items:
            raise ValueError(f"the vessel history lists no ships for {rng[0]} → {rng[1]}")
        rows = to_rows(items, f.filename)
        first = min(r.statement_date for r in rows)
        if first > max(rng[0], FIRST_DAY):
            # the list starts after the day asked for: if KPT has begun dropping its oldest calls, that first
            # day may be cut part-way, so it keeps what earlier runs stored
            if first > rng[0] + timedelta(days=2):
                log.warning("kpt_vessel_history: KPT's history now starts %s (asked from %s)", first, rng[0])
            rows = [r for r in rows if r.statement_date > first]
        return [r for r in rows if r.statement_date >= rng[0]]
