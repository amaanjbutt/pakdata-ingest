"""pbs_external_trade — PBS monthly external trade by commodity (export & import).

Source: https://www.pbs.gov.pk/external-trade-statistics/ links monthly Excel
workbooks `Export_<Month>-<YYYY>.xlsx` / `Import_<Month>-<YYYY>.xlsx` (same
/wp-content/uploads/2020/07/ path the SPI bulletins use). PBS rotates older
files off the page, so coverage is roughly the trailing year.

Sheet layout (verified against Export_June-2026.xlsx):
    col 0  SL.NO          col 1  COMMODITIES        col 2  UNIT
    cols 3-5    current month : QUANTITY, VALUE RUPEES, VALUE DOLLARS
    cols 6-8    prior month   : QUANTITY, VALUE RUPEES, VALUE DOLLARS
    cols 9-11   year-ago month
    cols 12-17  % change blocks (ignored — derivable)

Each workbook therefore carries **two** usable months, so we ingest the current
and prior month from every file: that doubles coverage and later files simply
upsert revised figures over earlier ones.

Rows form a hierarchy — group headings ('A  FOOD GROUP'), items (' 1.RICE ') and
sub-items ('   a) BASMATI'). Sub-items are stored parent-qualified
('1.RICE / a) BASMATI') so the commodity key stays unique.

Units are PBS's own: value_pkr_mn is Rs million, value_usd_th is US$ thousand.
"""
from __future__ import annotations

import calendar
import io
import re
from dataclasses import dataclass
from datetime import date

from app import db
from app.config import settings
from ingestion import alerting, storage
from ingestion.framework import FetchedFile, IngestionJob

INDEX_URL = "https://www.pbs.gov.pk/external-trade-statistics/"
_FILE_RE = re.compile(r"/(Export|Import)_([A-Za-z]+)-(\d{4})\.xlsx$", re.I)
# Sub-item labels look like 'a) BASMATI' / 'b) OTHERS'.
_SUBITEM_RE = re.compile(r"^[a-z]\)", re.I)
# A group heading has a single-letter SL.NO.
_GROUP_SL_RE = re.compile(r"^[A-Z]$")
# Header of the fiscal year-to-date block, e.g. 'JULY - JUNE,   2025-2026'.
_CUMULATIVE_RE = re.compile(r"[A-Z]{3,9}\s*-\s*[A-Z]{3,9}\s*,", re.I)

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


@dataclass
class TradeRow:
    flow: str
    commodity: str
    obs_date: date
    commodity_group: str | None
    unit: str | None
    quantity: float | None
    value_pkr_mn: float | None
    value_usd_th: float | None


def _num(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s or s in {"-", "--"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def prev_month(d: date) -> date:
    return month_end(d.year - 1, 12) if d.month == 1 else month_end(d.year, d.month - 1)


def parse_trade_xlsx(content: bytes, flow: str, month: date) -> list[TradeRow]:
    """Parse one monthly workbook into rows for the current and prior month.
    Pure — no DB/network."""
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(content), data_only=True).active
    periods = [(month, 3), (prev_month(month), 6)]  # (obs_date, first column)

    out: list[TradeRow] = []
    group: str | None = None
    parent: str | None = None
    seen: set[tuple[str, date]] = set()

    for row in ws.iter_rows(values_only=True):
        if not row or len(row) < 6:
            continue
        sl = _clean(row[0])
        label = _clean(row[1])

        # Each workbook stacks two blocks: the month, then a fiscal year-to-date
        # block headed e.g. "JULY - JUNE, 2025-2026". The cumulative figures are
        # NOT monthly observations, so stop when that header appears.
        if any(_CUMULATIVE_RE.search(_clean(c)) for c in row[:6] if c):
            break

        if not label:
            continue

        # A single-letter SL.NO marks a commodity group (e.g. 'A  FOOD GROUP').
        # These rows also carry the group's totals, so record the name *and*
        # keep the row as data.
        if _GROUP_SL_RE.match(sl):
            group = label
            parent = None

        if _SUBITEM_RE.match(label) and parent:
            commodity = f"{parent} / {label}"
        else:
            commodity = label
            if not _SUBITEM_RE.match(label):
                parent = label

        unit = _clean(row[2]) or None
        for obs_date, base in periods:
            if len(row) <= base + 2:
                continue
            qty, pkr, usd = (_num(row[base]), _num(row[base + 1]), _num(row[base + 2]))
            if qty is None and pkr is None and usd is None:
                continue
            key = (commodity, obs_date)
            if key in seen:
                continue
            seen.add(key)
            out.append(TradeRow(flow=flow, commodity=commodity, obs_date=obs_date,
                                commodity_group=group, unit=unit, quantity=qty,
                                value_pkr_mn=pkr, value_usd_th=usd))

    if not out:
        raise ValueError(f"no {flow} rows parsed from workbook")
    return out


class PbsExternalTradeJob(IngestionJob):
    name = "pbs_external_trade"
    source = "PBS"

    INCREMENTAL_FILES = 2  # newest export + import

    def _list_files(self) -> list[tuple[str, date, str]]:
        """[(flow, month, url)] newest first, from the trade statistics index."""
        from bs4 import BeautifulSoup

        html = self.http_get(INDEX_URL).decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        out: list[tuple[str, date, str]] = []
        for a in soup.find_all("a", href=True):
            m = _FILE_RE.search(a["href"])
            if not m:
                continue
            flow, month_name, year = m.groups()
            mi = _MONTHS.get(month_name.lower())
            if not mi:
                continue
            out.append((flow.lower(), month_end(int(year), mi), a["href"]))
        # de-duplicate, newest first
        uniq = {(f, d): u for f, d, u in out}
        return sorted(((f, d, u) for (f, d), u in uniq.items()),
                      key=lambda t: t[1], reverse=True)

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        files = self._list_files()
        if not backfill:
            # newest file per flow
            newest: dict[str, tuple[str, date, str]] = {}
            for f, d, u in files:
                newest.setdefault(f, (f, d, u))
            files = list(newest.values())
        out: list[FetchedFile] = []
        for flow, d, url in files:
            try:
                content = self.http_get(url)
            except Exception:
                continue
            if content[:2] != b"PK":  # xlsx is a zip
                continue
            out.append(FetchedFile(filename=f"{flow}_{d:%Y-%m}.xlsx",
                                   content=content, when=d))
        return out

    def parse(self, f: FetchedFile):
        flow = "export" if f.filename.startswith("export") else "import"
        return parse_trade_xlsx(f.content, flow, f.when)

    def validate(self, records):
        # Dedicated trade table — the generic catalog gates don't apply.
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        rows = [r for r, _ in validated]
        if not rows:
            return 0
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO trade_flows (flow, commodity, obs_date, commodity_group,
                                                 unit, quantity, value_pkr_mn, value_usd_th,
                                                 revised_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (flow, commodity, obs_date) DO UPDATE SET
                            commodity_group=EXCLUDED.commodity_group, unit=EXCLUDED.unit,
                            quantity=EXCLUDED.quantity, value_pkr_mn=EXCLUDED.value_pkr_mn,
                            value_usd_th=EXCLUDED.value_usd_th, revised_at=now()
                        """,
                        (r.flow, r.commodity, r.obs_date, r.commodity_group, r.unit,
                         r.quantity, r.value_pkr_mn, r.value_usd_th),
                    )
                    n += 1
        return n
