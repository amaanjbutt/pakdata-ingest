"""sindh_amis_prices - Sindh Agricultural Market Intelligence: daily fruit & vegetable rates.

Source: Sindh's AMIS portal (Sindh Agriculture Department; served from staging-amis.com and
new-theme.staging-amis.com — `SINDH_AMIS_URLS` below, tried in order)
    GET /market_rates?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD
returns every rate in the window as one table (`#dailyPricesTable`, paged client-side):

    S# | COMMODITY       | MARKET NAME      | DATE        | MARKET RATES
    1  | Turai, Grade 2  | Pano Akil Market | 16 Oct 2026 | PKR 70 / KG

The page moved from /market_price to /market_rates and dropped its DISTRICT column on
2026-10-23 (the job 404'd for three days), so columns are matched by header text and a
missing district never overwrites the stored one.

About 27 markets and 200 graded fruits and vegetables, daily from late August 2026. Each
rate is one market price (stored as `fqp`; Sindh publishes no min/max) in rupees per kg —
or per dozen where the commodity name says "Per Dozen" (bananas; the rate itself always
prints "/ KG"). The filter form's <select> options carry Sindh's
market and commodity ids; we store them offset by `ID_OFFSET` in the shared agri tables
(with `province = 'Sindh'`, slugs `sindh_karachi`, `sindh_potato_grade_1`) so they never
collide with Punjab AMIS ids or slugs.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta

from ingestion.framework import IngestionJob

SINDH_AMIS_URLS = ("https://new-theme.staging-amis.com/market_rates", "https://staging-amis.com/market_rates")
ID_OFFSET = 900_000
FIRST_DAY = date(2026, 8, 17)

_TABLE = re.compile(r'id="dailyPricesTable">(.*?)</table>', re.S)
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TH = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
# header text -> field; positional fallbacks for the 6-column (to 2026-10-22) and 5-column layouts
_HEADERS = {"commodity": "commodity", "market name": "market", "market": "market", "district": "district",
            "date": "date", "market rates": "rate", "market rate": "rate", "rate": "rate"}
_POSITIONS = {6: {"commodity": 1, "market": 2, "district": 3, "date": 4, "rate": 5},
              5: {"commodity": 1, "market": 2, "date": 3, "rate": 4}}
_RATE = re.compile(r"PKR\s*([\d,]+(?:\.\d+)?)\s*/\s*([A-Za-z ]+)", re.I)
_UNITS = {"kg": "pkr_per_kg", "dozen": "pkr_per_dozen", "piece": "pkr_each", "maund": "pkr_per_40kg"}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def parse_options(page: str, name: str) -> dict[str, int]:
    """{option label: id} for a filter <select> (first id wins for a repeated label). Pure."""
    m = re.search(r'<select[^>]*name="' + re.escape(name) + r'"[^>]*>(.*?)</select>', page, re.S)
    out: dict[str, int] = {}
    for val, label in re.findall(r'<option[^>]*value="(\d+)"[^>]*>(.*?)</option>', m.group(1) if m else "", re.S):
        out.setdefault(_text(label).lower(), int(val))
    return out


def parse_rates(page: str) -> list[dict]:
    """[{commodity, market, district, obs_date, price, unit}] from the rates table. Pure."""
    t = _TABLE.search(page)
    if not t:
        return []  # Sindh shows a "no records" message instead of the table
    heads = [_HEADERS.get(_text(h).lower()) for h in _TH.findall(t.group(1))]
    by_header = {f: i for i, f in enumerate(heads) if f}
    out = []
    for row in _ROW.findall(t.group(1)):
        c = [_text(x) for x in _TD.findall(row)]
        if not c:
            continue
        col = by_header if {"commodity", "market", "date", "rate"} <= set(by_header) else _POSITIONS.get(len(c))
        if not col or max(col.values()) >= len(c):
            continue
        rate = _RATE.search(c[col["rate"]])
        if not rate:
            continue
        price = float(rate.group(1).replace(",", ""))
        if price <= 0:
            continue
        commodity = c[col["commodity"]]
        unit = _UNITS.get(rate.group(2).strip().lower(), "pkr_per_" + re.sub(r"\W+", "_", rate.group(2).strip().lower()))
        if "per dozen" in commodity.lower():  # bananas are priced per dozen though the rate says "/ KG"
            unit = "pkr_per_dozen"
        district = c[col["district"]] if "district" in col else None
        out.append(dict(commodity=commodity, market=c[col["market"]], district=district or None,
                        obs_date=datetime.strptime(c[col["date"]], "%d %b %Y").date(), price=price, unit=unit))
    return out


def category_for(name: str, fruits: set[str]) -> str:
    return "Fruits" if name.lower() in fruits else "Vegetables"


class SindhAmisPricesJob(IngestionJob):
    """The last few days daily (Sindh enters a day's rates over the day); a backfill reads
    from FIRST_DAY month by month."""

    name = "sindh_amis_prices"
    source = "Sindh AMIS"
    lookback_days = 3

    def _get(self, params: str = "") -> str:
        """First URL in SINDH_AMIS_URLS that answers (the site has moved before)."""
        last: Exception | None = None
        for base in SINDH_AMIS_URLS:
            try:
                return self.http_get(base + params).decode("utf-8", "replace")
            except Exception as exc:  # 404 after a move, DNS gone, … — try the next host
                last = exc
        raise RuntimeError(f"every Sindh AMIS URL failed: {last}")

    def run(self, backfill: bool = False) -> dict:
        from ingestion.jobs.amis_prices import slug, upsert_prices

        run_id = self._start_run()
        try:
            today = date.today()
            if backfill:
                windows, d = [], FIRST_DAY
                while d <= today:
                    end = min(today, d + timedelta(days=30))
                    windows.append((d, end))
                    d = end + timedelta(days=1)
            else:
                windows = [(today - timedelta(days=self.lookback_days - 1), today)]
            pages = [self._get(f"?from_date={a}&to_date={b}") for a, b in windows]
            markets = parse_options(pages[0], "market_ids[]")
            commodities = parse_options(pages[0], "commodity_ids[]")
            # The filter's option lists ignore the category; the rates table honours it.
            fruits = {r["commodity"].lower() for r in parse_rates(
                self._get(f"?category_id=1&from_date={FIRST_DAY}&to_date={today}"))}
            if not markets or not commodities:
                raise RuntimeError("Sindh AMIS market/commodity lists came back empty (layout change?)")
            rates = [r for p in pages for r in parse_rates(p)]
            unknown = {r["commodity"] for r in rates if r["commodity"].lower() not in commodities} | \
                      {r["market"] for r in rates if r["market"].lower() not in markets}
            rows, ref_m, ref_c = [], {}, {}
            for r in rates:
                cid, mid = commodities.get(r["commodity"].lower()), markets.get(r["market"].lower())
                if cid is None or mid is None:
                    continue
                ref_m[mid] = (r["market"], r["district"])
                ref_c[cid] = (r["commodity"], r["unit"])
                rows.append(dict(commodity_id=ID_OFFSET + cid, market_id=ID_OFFSET + mid,
                                 obs_date=r["obs_date"], fqp=r["price"]))
            self._upsert_reference(ref_m, ref_c, fruits, slug)
            n = upsert_prices(rows)
            status = "success" if rows else "no_new_data"
            note = f"unmapped names: {sorted(unknown)[:5]}" if unknown else None
            self._finish(run_id, status, n, note, None)
            return {"status": status, "rows": n, "fetched_rows": len(rows), "unmapped": len(unknown)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc)[:500], None)
            from ingestion import alerting
            alerting.alert(f"{self.name}: job failed", str(exc)[:500])
            raise

    @staticmethod
    def _upsert_reference(markets: dict, commodities: dict, fruits: set[str], slug) -> None:
        from app import db

        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO agri_markets (market_id, name, slug, is_major, province, district)
                       VALUES (%s, %s, %s, false, 'Sindh', %s)
                       ON CONFLICT (market_id) DO UPDATE SET name = EXCLUDED.name, slug = EXCLUDED.slug,
                           province = 'Sindh', district = COALESCE(EXCLUDED.district, agri_markets.district)""",
                    [(ID_OFFSET + i, n, "sindh_" + slug(re.sub(r"\s+Market$", "", n)), d)
                     for i, (n, d) in markets.items()])
                cur.executemany(
                    """INSERT INTO agri_commodities (commodity_id, name, slug, category, unit, province)
                       VALUES (%s, %s, %s, %s, %s, 'Sindh')
                       ON CONFLICT (commodity_id) DO UPDATE SET name = EXCLUDED.name, slug = EXCLUDED.slug,
                           category = EXCLUDED.category, unit = EXCLUDED.unit, province = 'Sindh'""",
                    [(ID_OFFSET + i, n, "sindh_" + slug(n), category_for(n, fruits), u)
                     for i, (n, u) in commodities.items()])
            from ingestion.agri_names import apply_names
            apply_names(conn)
