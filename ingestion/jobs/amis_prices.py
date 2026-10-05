"""amis_prices — Punjab AMIS daily wholesale (mandi) prices.

AMIS (Agriculture Marketing Information Service, Directorate of Agriculture (E&M) Punjab,
www.amis.pk) publishes daily wholesale prices for ~136 commodities across ~143 Punjab
markets: per market and day, the Min, Max and **FQP** (fair-quality price, the
representative price) and sometimes arrival quantity, in Rs per 100 kg unless the
commodity name says otherwise (e.g. "Banana(DOZEN)"). Data starts 2007-05-07.

Sources (plain ASP.NET, reachable from the VPS — no browser needed):
- `ViewPrices.aspx?searchType=1&commodityId=<market_id>` — one market, one day, every
  commodity with Min/Max/FQP/Quantity. A past day = POST the page's own form with
  `DateTextBox=MM/DD/YYYY` + the "Show prices" button.
- `reports/CommodityChart.aspx` ("Price_Trends", SSRS) — one commodity × market, FQP only,
  any date range in one CSV export. Its crop ids differ from the site's; map by name.
  Used by the one-off history backfill (scripts/amis_backfill.py, run on a workstation).

Storage: a dedicated table (`agri_prices`, with `agri_markets`, `agri_commodities`) —
the full history is ~10M+ rows, too many for the generic `observations` store. Exposed
via `/v1/agri/*`.

Two jobs: `amis_prices` (major markets, last 3 days — daily) and `amis_prices_all` (every
market, last 8 days — weekly, at a quiet hour; user's call 2026-10-05).
"""
from __future__ import annotations

import csv
import html as _html
import io
import re
import time
from datetime import date, datetime, timedelta

from ingestion.framework import IngestionJob

BASE = "http://www.amis.pk"
CITY_URL = BASE + "/ViewPrices.aspx?searchType=1&commodityId={market}"
COMMODITY_URL = BASE + "/ViewPrices.aspx?searchType=0&commodityId={commodity}"
TRENDS_URL = BASE + "/reports/CommodityChart.aspx?cmd={crop}&city={market}"
DATA_START = date(2007, 5, 7)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
_MAX_PRICE = 5_000_000  # Rs/100kg; anything above is a typo

# The big mandis (matched by AMIS market name, case/space-insensitive): fetched daily.
MAJOR_MARKETS = [
    "Lahore", "Faisalabad", "Rawalpindi", "Multan", "Gujranwala", "Sialkot", "Sargodha", "Bahawalpur",
    "Sahiwal", "Okara", "RahimYarKhan", "DGKhan", "Jhang", "Sheikhupura", "Kasur", "Gujrat",
    "Vehari", "Khanewal", "BahawalNagar", "MuzafarGhar", "Chiniot", "Mianwali", "Karachi",
]

_ROW = re.compile(
    r"<a href='[^']*searchType=(\d)&commodityId=(\d+)'>([^<]+)</a>.*?"
    r"CommodityChart\.aspx\?cmd=(\d+)&city=(\d+)\">Graph</a></td>\s*"
    r"<td[^>]*>&nbsp;([^<]*)</td>\s*<td[^>]*>&nbsp;([^<]*)</td>\s*"
    r"<td[^>]*>&nbsp;([^<]*)</td>\s*<td[^>]*>&nbsp;([^<]*)</td>", re.S)
_GROUP = re.compile(r"<td colspan=7[^>]*>(?:<[^>]+>)*([A-Za-z &]+)<", re.S)
_DATED = re.compile(r"Dated:(\d{2})-(\d{2})-(\d{4})")
_LISTBOX = re.compile(r'name="ctl00\$cphPage\$ListBox2\$\d+" value="(\d+)" /><label[^>]*>([^<]+)</label>')


def _num(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("\xa0", "").strip()
    if not s or s == "-":
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if 0 < v < _MAX_PRICE else None


def slug(name: str) -> str:
    s = re.sub(r"[^\x00-\x7f]", "", _html.unescape(name)).lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(name).replace("\xa0", " ")).strip()


def unit_for(name: str) -> str:
    n = name.lower()
    if "dozen" in n:
        return "pkr_per_dozen"
    return "pkr_per_100kg"


def parse_day_page(page: str) -> tuple[date | None, list[dict]]:
    """A ViewPrices page (one market, one day — or one commodity, all markets) ->
    (date, [{commodity_id, commodity, category, market_id, min, max, fqp, quantity}])."""
    m = _DATED.search(page)
    when = date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None
    # category headers (Grains / Vegetables / Fruits) precede their rows
    marks = sorted([(g.start(), clean_name(g.group(1))) for g in _GROUP.finditer(page)])
    rows = []
    for r in _ROW.finditer(page):
        cat = None
        for pos, name in marks:
            if pos < r.start():
                cat = name
        mn, mx, fqp, qty = (_num(x) for x in r.groups()[5:9])
        if mn is None and mx is None and fqp is None:
            continue
        rows.append({"commodity_id": int(r.group(4)), "market_id": int(r.group(5)),
                     "commodity": clean_name(r.group(3)) if r.group(1) == "0" else None,
                     "category": cat, "min": mn, "max": mx, "fqp": fqp, "quantity": qty})
    return when, rows


def parse_listbox(page: str) -> dict[int, str]:
    """ViewPrices' checkbox list: on a market page it lists commodities, on a commodity
    page it lists markets. -> {id: name}."""
    return {int(v): clean_name(n) for v, n in _LISTBOX.findall(page)}


def parse_trends_csv(text: str) -> list[tuple[date, float]]:
    """Price_Trends CSV export -> [(date, fqp)] (blank days dropped)."""
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) == 3 and row[0] == "Price" and row[2].strip():
            v = _num(row[2])
            if v is not None:
                out.append((datetime.strptime(row[1], "%d %b %y").date(), v))
    return out


def hidden_fields(page: str) -> dict[str, str]:
    return {k: _html.unescape(v) for k, v in
            re.findall(r'<input[^>]*type="hidden"[^>]*name="([^"]+)"[^>]*value="([^"]*)"', page)}


# ---- HTTP ---------------------------------------------------------------------------------

class AmisClient:
    def __init__(self, delay: float = 0.4, timeout: float = 120):
        import httpx

        self.c = httpx.Client(headers={"User-Agent": UA}, timeout=timeout, follow_redirects=True)
        self.delay = delay
        self._form: dict[int, tuple[str, dict]] = {}

    def _get(self, url: str) -> str:
        time.sleep(self.delay)
        r = self.c.get(url)
        r.raise_for_status()
        return r.text

    def market_day(self, market_id: int, day: date | None = None) -> tuple[date | None, list[dict]]:
        """One market's prices for `day` (today if None)."""
        url = CITY_URL.format(market=market_id)
        if market_id not in self._form:
            page = self._get(url)
            self._form[market_id] = (url, hidden_fields(page))
            if day is None:
                return parse_day_page(page)
        if day is None:
            return parse_day_page(self._get(url))
        form = dict(self._form[market_id][1])
        form.update({"ctl00$cphPage$DateTextBox": day.strftime("%m/%d/%Y"),
                     "ctl00$cphPage$ReminderButton": "Show prices"})
        time.sleep(self.delay)
        r = self.c.post(url, data=form)
        r.raise_for_status()
        when, rows = parse_day_page(r.text)
        return (when if when == day else day if when is None else when), rows

    def markets(self) -> dict[int, str]:
        return parse_listbox(self._get(COMMODITY_URL.format(commodity=1)))

    def commodities(self) -> dict[int, str]:
        return parse_listbox(self._get(CITY_URL.format(market=1)))

    def trends(self, crop_id: int, market_id: int, start: date, end: date) -> list[tuple[date, float]]:
        """FQP history for one SSRS crop id × market (one CSV export)."""
        import codecs

        url = TRENDS_URL.format(crop=crop_id, market=market_id)
        form = hidden_fields(self._get(url))
        form.update({"ReportViewer1$ctl08$ctl03$ddValue": str(crop_id),
                     "ReportViewer1$ctl08$ctl05$ddValue": str(market_id),
                     "ReportViewer1$ctl08$ctl07$txtValue": f"{start.month}/{start.day}/{start.year}",
                     "ReportViewer1$ctl08$ctl09$txtValue": f"{end.month}/{end.day}/{end.year}",
                     "ReportViewer1$ctl08$ctl00": "View Report"})
        time.sleep(self.delay)
        page = self.c.post(url, data=form).text
        m = re.search(r'"ExportUrlBase":"([^"]+)"', page)
        if not m:
            return []
        r = self.c.get(BASE + codecs.decode(m.group(1), "unicode_escape") + "CSV")
        return parse_trends_csv(r.content.decode("utf-8-sig", "replace"))

    def trend_options(self) -> tuple[dict[int, str], dict[int, str]]:
        """The Price_Trends report's own crop and market ids -> names. Both differ from the
        site's ids (report market 13 = Vehari, site 13 = Sahiwal): map by name."""
        page = self._get(TRENDS_URL.format(crop=1, market=1))

        def opts(param: str) -> dict[int, str]:
            sel = re.search(r'name="ReportViewer1\$ctl08\$' + param + r'\$ddValue".*?</select>', page, re.S).group(0)
            return {int(v): clean_name(n) for v, n in re.findall(r'<option[^>]*value="(\d+)"[^>]*>([^<]*)', sel)}

        return opts("ctl03"), opts("ctl05")


# ---- storage ------------------------------------------------------------------------------

def upsert_reference(markets: dict[int, str], commodities: dict[int, tuple[str, str | None]]) -> None:
    from app import db

    major = {slug(m) for m in MAJOR_MARKETS}
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO agri_markets (market_id, name, slug, is_major) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (market_id) DO UPDATE SET name = EXCLUDED.name, slug = EXCLUDED.slug,
                       is_major = EXCLUDED.is_major""",
                [(i, n, slug(n), slug(n) in major) for i, n in markets.items()])
            cur.executemany(
                """INSERT INTO agri_commodities (commodity_id, name, slug, category, unit) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (commodity_id) DO UPDATE SET name = EXCLUDED.name, slug = EXCLUDED.slug,
                       category = COALESCE(EXCLUDED.category, agri_commodities.category), unit = EXCLUDED.unit""",
                [(i, n, slug(n), cat, unit_for(n)) for i, (n, cat) in commodities.items()])


def upsert_prices(rows: list[dict]) -> int:
    """rows: {commodity_id, market_id, obs_date, min, max, fqp, quantity}. A NULL never
    overwrites a stored value (the FQP-only history backfill must not erase min/max)."""
    from app import db

    if not rows:
        return 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS _agri_in (commodity_id int, market_id int, obs_date date, "
                        "min_price numeric, max_price numeric, fqp numeric, quantity numeric) ON COMMIT DROP")
            with cur.copy("COPY _agri_in FROM STDIN") as cp:
                for r in rows:
                    cp.write_row((r["commodity_id"], r["market_id"], r["obs_date"], r.get("min"), r.get("max"),
                                  r.get("fqp"), r.get("quantity")))
            cur.execute(
                """INSERT INTO agri_prices AS p (commodity_id, market_id, obs_date, min_price, max_price, fqp, quantity)
                   SELECT DISTINCT ON (commodity_id, market_id, obs_date) * FROM _agri_in
                   WHERE commodity_id IN (SELECT commodity_id FROM agri_commodities)
                     AND market_id IN (SELECT market_id FROM agri_markets)
                   ON CONFLICT (commodity_id, market_id, obs_date) DO UPDATE SET
                       min_price = COALESCE(EXCLUDED.min_price, p.min_price),
                       max_price = COALESCE(EXCLUDED.max_price, p.max_price),
                       fqp = COALESCE(EXCLUDED.fqp, p.fqp),
                       quantity = COALESCE(EXCLUDED.quantity, p.quantity),
                       revised_at = now()
                   WHERE (EXCLUDED.min_price, EXCLUDED.max_price, EXCLUDED.fqp, EXCLUDED.quantity)
                         IS DISTINCT FROM (p.min_price, p.max_price, p.fqp, p.quantity)""")
            return cur.rowcount


# ---- jobs ---------------------------------------------------------------------------------

class AmisPricesJob(IngestionJob):
    """Major markets, the last few days (AMIS fills a day in over the evening; re-reading
    the previous days catches late entries and corrections)."""

    name = "amis_prices"
    source = "AMIS"
    all_markets = False
    lookback_days = 3

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            cl = AmisClient()
            markets = cl.markets()
            names = cl.commodities()
            if not markets or not names:
                raise RuntimeError("AMIS market/commodity lists came back empty (layout change?)")
            major = {slug(m) for m in MAJOR_MARKETS}
            todo = [i for i, n in markets.items() if self.all_markets or slug(n) in major]
            days = [date.today() - timedelta(days=k) for k in range(self.lookback_days)]
            rows, cats = [], {}
            failures = 0
            for mid in todo:
                for d in days:
                    try:
                        when, got = cl.market_day(mid, d)
                    except Exception:  # noqa: BLE001 — one market/day must not sink the run
                        failures += 1
                        continue
                    for r in got:
                        cats.setdefault(r["commodity_id"], r["category"])
                        rows.append({**r, "obs_date": when or d})
            upsert_reference(markets, {i: (n, cats.get(i)) for i, n in names.items()})
            n = upsert_prices(rows)
            status = "success" if rows else "no_new_data"
            if failures and failures >= len(todo) * len(days) // 2:
                status = "partial"
            self._finish(run_id, status, n, f"{failures} market-day fetches failed" if failures else None, None)
            return {"status": status, "rows": n, "markets": len(todo), "fetched_rows": len(rows), "failures": failures}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc)[:500], None)
            raise


class AmisPricesAllJob(AmisPricesJob):
    """Every market, the last 8 days — weekly at a quiet hour."""

    name = "amis_prices_all"
    all_markets = True
    lookback_days = 8
