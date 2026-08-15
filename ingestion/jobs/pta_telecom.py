"""pta_telecom - PTA telecom indicators, multi-frequency.

Source: PTA publishes telecom indicators as Highcharts charts, but the chart
config is embedded in the server-rendered HTML - `"categories":[...]` (period
labels) and `"series":[{"name":...,"data":[...]}]` - so we parse it directly,
no JS execution or API needed.

Each indicator is a category page under /category/telecom-indicators/<id>. The
`INDICATORS` table is the single source of truth for both parsing (chart series
name -> catalog id) and catalog seeding (name/unit/bounds), consumed by
`db.init._seed_telecom_catalog()`.

Period labels come in three shapes, selected per indicator by `freq`:
  - monthly:   "Jul-25"                 -> 2025-07-31
  - quarterly: "Jul-Sep 22", "Jan-Mar 26" -> quarter-end (2022-09-30, 2026-03-31)
  - annual:    "2018-19 (R)", "2024-25 (P)" -> fiscal-year end (2019-06-30, 2025-06-30)

Only a rolling window is shown per page, so history accrues as the monthly job
runs; upserts keep it idempotent. Pages that are not time series (operator
market-share pie, complaints-by-service snapshot, mixed-label device
manufacturing) are intentionally not modelled here.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime

from ingestion.framework import FetchedFile, IngestionJob, Record

BASE = "https://www.pta.gov.pk/category/telecom-indicators/{id}"


class S:
    """One catalog series: its chart-series name plus seeding metadata."""

    __slots__ = ("chart_name", "id", "name", "unit", "min_value", "max_value")

    def __init__(self, chart_name, sid, name, unit, min_value, max_value):
        self.chart_name = chart_name
        self.id = sid
        self.name = name
        self.unit = unit
        self.min_value = min_value
        self.max_value = max_value


# page id + frequency + the series we extract from it.
INDICATORS: dict[str, dict] = {
    "teledensity": {"id": 165, "freq": "monthly", "series": [
        S("Total", "telecom.teledensity.total", "Teledensity - Total", "percent", 0, 200),
        S("Cellular Mobile", "telecom.teledensity.cellular", "Teledensity - Cellular", "percent", 0, 200),
        S("FLL & WLL", "telecom.teledensity.fixed", "Teledensity - Fixed (FLL/WLL)", "percent", 0, 200),
    ]},
    "subscribers": {"id": 164, "freq": "monthly", "series": [
        S("Total", "telecom.subscribers.cellular.total", "Cellular Subscribers - Total", "count", 0, 500_000_000),
        S("Jazz", "telecom.subscribers.cellular.jazz", "Cellular Subscribers - Jazz", "count", 0, 300_000_000),
        S("Zong", "telecom.subscribers.cellular.zong", "Cellular Subscribers - Zong", "count", 0, 300_000_000),
        S("Telenor", "telecom.subscribers.cellular.telenor", "Cellular Subscribers - Telenor", "count", 0, 300_000_000),
        S("Ufone", "telecom.subscribers.cellular.ufone", "Cellular Subscribers - Ufone", "count", 0, 300_000_000),
        S("SCO", "telecom.subscribers.cellular.sco", "Cellular Subscribers - SCO", "count", 0, 50_000_000),
    ]},
    "revenues": {"id": 168, "freq": "annual", "series": [
        S("Total", "telecom.revenue.total", "Telecom Revenue - Total", "pkr_bn", 0, 10000),
        S("CMO", "telecom.revenue.cmo", "Telecom Revenue - Cellular Mobile Operators", "pkr_bn", 0, 10000),
        S("FLL / WLL", "telecom.revenue.fll_wll", "Telecom Revenue - FLL/WLL", "pkr_bn", 0, 5000),
        S("LDI", "telecom.revenue.ldi", "Telecom Revenue - LDI", "pkr_bn", 0, 5000),
        S("TTP/TIP", "telecom.revenue.ttp_tip", "Telecom Revenue - TTP/TIP", "pkr_bn", 0, 5000),
        S("CVAS", "telecom.revenue.cvas", "Telecom Revenue - CVAS", "pkr_bn", 0, 5000),
    ]},
    "investments": {"id": 169, "freq": "annual", "series": [
        S("Total", "telecom.investment.total", "Telecom Investment - Total", "usd_mn", 0, 20000),
        S("CMO", "telecom.investment.cmo", "Telecom Investment - Cellular Mobile Operators", "usd_mn", 0, 20000),
        S("LDI", "telecom.investment.ldi", "Telecom Investment - LDI", "usd_mn", 0, 10000),
        S("TTP/TIP", "telecom.investment.ttp_tip", "Telecom Investment - TTP/TIP", "usd_mn", 0, 10000),
        S("FLL/CVAS", "telecom.investment.fll_cvas", "Telecom Investment - FLL/CVAS", "usd_mn", 0, 10000),
    ]},
    "fdi": {"id": 170, "freq": "annual", "series": [
        S("Inflow", "telecom.fdi.inflow", "Telecom FDI - Inflow", "usd_mn", 0, 10000),
        S("Outflow", "telecom.fdi.outflow", "Telecom FDI - Outflow", "usd_mn", 0, 10000),
        S("Net FDI", "telecom.fdi.net", "Telecom FDI - Net", "usd_mn", -5000, 10000),
    ]},
    "exchequer": {"id": 167, "freq": "annual", "series": [
        S("Total", "telecom.exchequer.total", "Telecom Exchequer Contribution - Total", "pkr_bn", 0, 5000),
        S("GST", "telecom.exchequer.gst", "Telecom Exchequer Contribution - GST", "pkr_bn", 0, 5000),
        S("Others", "telecom.exchequer.others", "Telecom Exchequer Contribution - Others", "pkr_bn", 0, 5000),
        S("PTA Deposits", "telecom.exchequer.pta_deposits", "Telecom Exchequer Contribution - PTA Deposits", "pkr_bn", 0, 5000),
    ]},
    "cell_sites": {"id": 173, "freq": "quarterly", "series": [
        S("2G", "telecom.cell_sites.2g", "Cellular Cell Sites - 2G", "count", 0, 500_000),
        S("3G", "telecom.cell_sites.3g", "Cellular Cell Sites - 3G", "count", 0, 500_000),
        S("4G", "telecom.cell_sites.4g", "Cellular Cell Sites - 4G", "count", 0, 500_000),
    ]},
    "arpu": {"id": 174, "freq": "quarterly", "series": [
        S("ARPU", "telecom.arpu", "Cellular ARPU (Average Revenue Per User)", "pkr", 0, 5000),
    ]},
    "broadband_data": {"id": 175, "freq": "quarterly", "series": [
        S("Petabytes", "telecom.broadband.data_petabytes", "Mobile Broadband Data Usage", "petabytes", 0, 200_000),
    ]},
    "bandwidth": {"id": 177, "freq": "monthly", "series": [
        S("Maximum", "telecom.bandwidth.max", "International Bandwidth Utilisation - Maximum", "tbps", 0, 100),
        S("Minimum", "telecom.bandwidth.min", "International Bandwidth Utilisation - Minimum", "tbps", 0, 100),
        S("Average", "telecom.bandwidth.avg", "International Bandwidth Utilisation - Average", "tbps", 0, 100),
    ]},
}

ALL_SERIES = [s.id for ind in INDICATORS.values() for s in ind["series"]]

_CATEGORIES_RE = re.compile(r'"categories"\s*:\s*(\[[^\]]*\])')
_SERIES_RE = re.compile(r'"name"\s*:\s*"([^"]+)"[^}]*?"data"\s*:\s*(\[[^\]]*\])')
_FISCAL_RE = re.compile(r"^\s*(\d{4})-\d{2}")
_QUARTER_RE = re.compile(r"[A-Za-z]{3}-([A-Za-z]{3})\s+(\d{2})")
_MONTH_NUM = {m: i for i, m in enumerate(calendar.month_abbr) if m}


def _norm_name(raw: str) -> str:
    """Highcharts names carry JSON-escaped slashes (``FLL \\/ WLL``)."""
    return raw.replace("\\/", "/").strip()


def _month_end_ym(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _label_to_date(label: str, freq: str) -> date | None:
    """Map a chart x-axis label to an observation date, per indicator frequency."""
    label = str(label).strip()
    if freq == "monthly":  # 'Jul-25'
        try:
            d = datetime.strptime(label, "%b-%y")
        except ValueError:
            return None
        return _month_end_ym(d.year, d.month)
    if freq == "quarterly":  # 'Jul-Sep 22' -> end month of the quarter
        m = _QUARTER_RE.search(label)
        if not m:
            return None
        month = _MONTH_NUM.get(m.group(1).title())
        if not month:
            return None
        return _month_end_ym(2000 + int(m.group(2)), month)
    if freq == "annual":  # fiscal year '2024-25 (P)' -> 30 Jun of the later year
        m = _FISCAL_RE.match(label)
        if not m:
            return None
        return date(int(m.group(1)) + 1, 6, 30)
    return None


def parse_pta_chart(html: str, indicator: dict) -> list[Record]:
    """Parse the primary chart on a PTA indicator page. Pure - no DB/net.

    Uses the first `categories` array (the primary chart's x-axis) and, for each
    mapped series, the first matching same-length data run. Where a page carries
    several charts sharing series names (e.g. subscriber counts then market
    share), the primary chart appears first, so first-match wins."""
    cm = _CATEGORIES_RE.search(html)
    if not cm:
        raise ValueError("no chart categories found on PTA page")
    labels = json.loads(cm.group(1))
    freq = indicator["freq"]
    dates = [_label_to_date(x, freq) for x in labels]
    n = len(dates)
    by_name = {s.chart_name: s.id for s in indicator["series"]}

    records: list[Record] = []
    seen: set[str] = set()
    for m in _SERIES_RE.finditer(html, cm.start()):
        sid = by_name.get(_norm_name(m.group(1)))
        if sid is None or sid in seen:
            continue
        data = json.loads(m.group(2))
        if len(data) != n:
            continue
        seen.add(sid)
        for d, v in zip(dates, data):
            if d is None or v is None:
                continue
            records.append(Record(sid, d, float(v), {}))
        if len(seen) == len(by_name):
            break

    if not records:
        raise ValueError("no PTA series parsed")
    return records


class PtaTelecomJob(IngestionJob):
    name = "pta_telecom"
    source = "PTA"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        for key, ind in INDICATORS.items():
            try:
                content = self.http_get(BASE.format(id=ind["id"]))
            except Exception:
                continue
            out.append(FetchedFile(filename=f"{key}.html", content=content,
                                   when=date.today()))
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        key = f.filename.rsplit(".", 1)[0]
        return parse_pta_chart(f.content.decode("utf-8", errors="replace"),
                               INDICATORS[key])
