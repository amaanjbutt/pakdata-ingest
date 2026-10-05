"""derive_indicators — the PakDataHub derived layer (deterministic, every plan).

Indicators no publisher prints, computed from official series already in our database
with a published formula: spliced long histories, real interest rates, spreads, import
cover, rolling sums, city essentials inflation, fund-category yields. A pure DB
derivation (no fetch), re-run after the inputs land; every point is recomputed each run
(idempotent upsert), so a revised input revises the derived value too — and the
revision trigger records it like any other.

Labelling: `series.source = 'PakDataHub'` and `series.derivation` (JSONB) =
{"method": ..., "inputs": [...], "version": N} — exposed as `derived` on catalog records
and series `meta`. The official layer is never modelled; this layer is clearly marked.

Each indicator is a `Spec` with a pure `compute(inputs) -> [(date, value)]` so the
formulas are unit-tested without a database (tests/test_derive_indicators.py).
"""
from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from app import db
from ingestion.framework import IngestionJob

Points = list[tuple[date, float]]

# ---- pure helpers -----------------------------------------------------------------------


def month_end(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return nxt - timedelta(days=1)


def splice(*sources: Points) -> Points:
    """Merge series by priority: for each date the FIRST source that has it wins."""
    out: dict[date, float] = {}
    for src in reversed(sources):
        out.update(dict(src))
    return sorted(out.items())


def step_asof(changes: Points, dates: list[date]) -> Points:
    """Value of a step series (e.g. a policy rate, dated at each change) in effect on
    each date. Dates before the first change are skipped."""
    ch = sorted(changes)
    out, i, cur = [], 0, None
    for d in sorted(dates):
        while i < len(ch) and ch[i][0] <= d:
            cur = ch[i][1]
            i += 1
        if cur is not None:
            out.append((d, cur))
    return out


def month_ends_between(first: date, last: date) -> list[date]:
    out, d = [], month_end(first)
    while d <= last:
        out.append(d)
        d = month_end(d + timedelta(days=1))
    return out


def monthly_mean(points: Points, min_obs: int = 1) -> Points:
    by: dict[date, list[float]] = defaultdict(list)
    for d, v in points:
        by[month_end(d)].append(v)
    return sorted((m, sum(vs) / len(vs)) for m, vs in by.items() if len(vs) >= min_obs)


def difference(a: Points, b: Points) -> Points:
    """a - b on common dates."""
    bd = dict(b)
    return [(d, v - bd[d]) for d, v in a if d in bd]


def difference_asof(a: Points, b_changes: Points) -> Points:
    """a - (step series b in effect on each of a's dates)."""
    b = dict(step_asof(b_changes, [d for d, _ in a]))
    return [(d, v - b[d]) for d, v in a if d in b]


def rolling_sum_months(points: Points, n: int = 12) -> Points:
    """Sum of the last n CONSECUTIVE monthly values (gaps break the window)."""
    by = {month_end(d): v for d, v in points}
    out = []
    for m in sorted(by):
        window, d = [], m
        for _ in range(n):
            if d not in by:
                break
            window.append(by[d])
            d = month_end(date(d.year, d.month, 1) - timedelta(days=1))
        if len(window) == n:
            out.append((m, sum(window)))
    return out


def ratio_to_trailing_mean(num: Points, den: Points, n: int = 3) -> Points:
    """num[t] / mean(den[t-n+1..t]) on month-ends (consecutive months required)."""
    dd = {month_end(d): v for d, v in den}
    out = []
    for d, v in num:
        m = month_end(d)
        window, x = [], m
        for _ in range(n):
            if x not in dd:
                break
            window.append(dd[x])
            x = month_end(date(x.year, x.month, 1) - timedelta(days=1))
        if len(window) == n and sum(window) > 0:
            out.append((m, v / (sum(window) / n)))
    return out


def yoy_weekly(points: Points, tolerance_days: int = 3) -> Points:
    """Year-on-year % change of a weekly series: vs the observation 364±3 days earlier."""
    pts = sorted(points)
    by = dict(pts)
    offsets = [364] + [364 + s * k for k in range(1, tolerance_days + 1) for s in (-1, 1)]
    out = []
    for d, v in pts:
        for back in offsets:
            prev = by.get(d - timedelta(days=back))
            if prev:
                out.append((d, (v / prev - 1.0) * 100.0))
                break
    return out


def share_pct(part: Points, whole: Points, whole_scale: float = 1.0) -> Points:
    wd = dict(whole)
    return [(d, v / (wd[d] * whole_scale) * 100.0) for d, v in part if wd.get(d)]


def weekly_median(rows: list[tuple[int, date, float]], min_funds: int = 5) -> Points:
    """rows (fund_id, obs_date, value) -> per week (ending Friday) the median over funds
    of each fund's LATEST value that week; weeks with < min_funds funds are skipped."""
    latest: dict[date, dict[int, tuple[date, float]]] = defaultdict(dict)
    for fid, d, v in rows:
        if v is None:
            continue
        week_end = d + timedelta(days=(4 - d.weekday()) % 7)
        cur = latest[week_end].get(fid)
        if cur is None or d > cur[0]:
            latest[week_end][fid] = (d, float(v))
    return sorted((w, statistics.median(v for _d, v in funds.values()))
                  for w, funds in latest.items() if len(funds) >= min_funds)


# ---- specs ------------------------------------------------------------------------------

@dataclass
class Spec:
    id: str
    name: str
    module: str
    unit: str
    frequency: str
    method: str
    inputs: list[str]
    compute: Callable[[dict], Points]
    bounds: tuple[float, float] = (-1e18, 1e18)
    version: int = 1
    extra: dict = field(default_factory=dict)


# Input keys (resolved by load_inputs): plain series ids, "<id>|side=offer" for one dims
# value, and "funds:<category>:<column>" for fund-return rows.
CPI_YOY = ["inflation.cpi.national.yoy", "inflation.national_cpi_inflation_measure_year_year",
           "industry.general_cpi_inflation_measure_year_year_2"]
POLICY = ["rates.reverse_repo", "rates.policy_target", "rates.policy"]
POLICY_TARGET_START = date(2015, 5, 25)  # SBP adopted the target-rate corridor; before it the reverse repo was the policy rate


def policy_changes(i: dict) -> Points:
    """Policy-rate history as dated changes: reverse repo before the May-2015 corridor
    framework, the target (policy) rate since; our daily scrape for the latest."""
    rr = [(d, v) for d, v in i["rates.reverse_repo"] if d < POLICY_TARGET_START]
    return splice(i["rates.policy"], i["rates.policy_target"], rr)


def policy_month_end(i: dict) -> Points:
    ch = policy_changes(i)
    if not ch:
        return []
    last = month_end(date.today().replace(day=1) - timedelta(days=1))  # last completed month
    return step_asof(ch, month_ends_between(ch[0][0], last))


def cpi_yoy(i: dict) -> Points:
    return splice(*(i[s] for s in CPI_YOY))


SPECS: list[Spec] = [
    Spec("inflation.cpi.national.yoy.long", "CPI inflation (national, YoY) — long history since 1964",
         "prices", "percent", "monthly",
         "Spliced: PBS national CPI YoY where published (Jul-2017 →), else SBP's base-2015-16 national CPI "
         "YoY (Jul-2016 →), else SBP's historical general CPI YoY (1964 → Apr-2020). One continuous series; "
         "base years differ across the splice points.",
         CPI_YOY, cpi_yoy, (-30, 60)),
    Spec("rates.policy.history", "SBP policy rate — month-end, since 1956",
         "fixed-income", "percent", "monthly",
         "The policy rate in effect at each month-end: SBP's reverse repo rate (the policy rate) until the "
         "interest-rate corridor of 25-May-2015, the policy (target) rate since.",
         POLICY, policy_month_end, (0, 40)),
    Spec("rates.real.policy", "Real policy rate (policy rate minus CPI inflation)",
         "fixed-income", "percent", "monthly",
         "Ex-post real policy rate = SBP policy rate at month-end − national CPI inflation (YoY) for the month "
         "(rates.policy.history − inflation.cpi.national.yoy.long).",
         POLICY + CPI_YOY, lambda i: difference(policy_month_end(i), cpi_yoy(i)), (-40, 40)),
    Spec("rates.real.kibor_3m", "Real 3-month KIBOR (KIBOR minus CPI inflation)",
         "fixed-income", "percent", "monthly",
         "Monthly average of the daily 3-month KIBOR offer rate (months with ≥10 fixings) − national CPI "
         "inflation (YoY) for the month.",
         ["rates.kibor.3m|side=offer"] + CPI_YOY,
         lambda i: difference(monthly_mean(i["rates.kibor.3m|side=offer"], 10), cpi_yoy(i)), (-40, 40)),
    Spec("rates.spread.kibor_3m_policy", "3-month KIBOR minus policy rate (spread)",
         "fixed-income", "percent", "daily",
         "Daily 3-month KIBOR offer − SBP policy rate in effect that day (reverse repo before 25-May-2015). "
         "Positive = the interbank market prices rates above the policy rate.",
         ["rates.kibor.3m|side=offer"] + POLICY,
         lambda i: difference_asof(i["rates.kibor.3m|side=offer"], policy_changes(i)), (-15, 15)),
    Spec("rates.spread.tbill_3m_policy", "3-month T-bill cut-off minus policy rate (spread)",
         "fixed-income", "percent", "irregular",
         "3-month T-bill auction cut-off yield − SBP policy rate in effect on the auction date.",
         ["rates.auction.tbill.3m.cutoff_yield"] + POLICY,
         lambda i: difference_asof(i["rates.auction.tbill.3m.cutoff_yield"], policy_changes(i)), (-15, 15)),
    Spec("rates.spread.pkrv_10y_3m", "PKRV yield-curve slope: 10-year minus 3-month",
         "fixed-income", "percent", "daily",
         "PKRV 10-year − PKRV 3-month (MUFAP revaluation rates), daily. Negative = inverted curve.",
         ["rates.pkrv.10y", "rates.pkrv.3m"],
         lambda i: difference(i["rates.pkrv.10y"], i["rates.pkrv.3m"]), (-15, 15)),
    Spec("rates.spread.pkrv_10y_1y", "PKRV yield-curve slope: 10-year minus 1-year",
         "fixed-income", "percent", "daily",
         "PKRV 10-year − PKRV 1-year (MUFAP revaluation rates), daily.",
         ["rates.pkrv.10y", "rates.pkrv.1y"],
         lambda i: difference(i["rates.pkrv.10y"], i["rates.pkrv.1y"]), (-15, 15)),
    Spec("fx.spread.interbank.usd", "USD/PKR interbank bid–offer spread",
         "forex", "pkr", "daily",
         "SBP weighted-average interbank USD/PKR offer − bid, daily (rupees).",
         ["fx.rate.interbank.usd|side=offer", "fx.rate.interbank.usd|side=bid"],
         lambda i: difference(i["fx.rate.interbank.usd|side=offer"], i["fx.rate.interbank.usd|side=bid"]), (-5, 20)),
    Spec("external.import_cover_months", "Import cover of SBP reserves (months)",
         "external", "months", "monthly",
         "SBP's total reserves (gold and foreign exchange) at month-end ÷ the average monthly import bill (goods and services, "
         "BOP) of the latest three months. How many months of imports the central bank's reserves pay for.",
         ["reserves.total_sbp_reserves", "bop.imports_goods_services_total"],
         lambda i: ratio_to_trailing_mean(i["reserves.total_sbp_reserves"], i["bop.imports_goods_services_total"], 3),
         (0, 24)),
    Spec("remittances.total.12m", "Workers' remittances — 12-month rolling total",
         "external", "usd_mn", "monthly",
         "Sum of the latest 12 consecutive months of workers' remittances (US$ million) — the annualised "
         "inflow, free of Ramadan/Eid seasonality.",
         ["remittances.total"], lambda i: rolling_sum_months(i["remittances.total"], 12), (0, 1e6)),
    Spec("payments.raast.share_of_retail_value", "Raast share of retail payments value",
         "alternate", "percent", "quarterly",
         "Raast transaction value (PKR bn) ÷ total retail payments value (PKR tn × 1,000) × 100, per quarter "
         "(SBP Payment Systems Review).",
         ["payments.raast.total.value", "payments.retail.value"],
         lambda i: share_pct(i["payments.raast.total.value"], i["payments.retail.value"], 1000.0), (0, 100)),
]

_FUND_CATEGORIES = {
    "money_market": ("Money Market", "d30", "Money-market funds — median 30-day annualised return",
                     "Median, across conventional money-market funds, of each fund's latest MUFAP 30-day "
                     "return (annualised, as MUFAP publishes it) in the week; weeks with <5 funds skipped."),
    "shariah_money_market": ("Shariah Compliant Money Market", "d30",
                             "Shariah money-market funds — median 30-day annualised return",
                             "Median, across Shariah-compliant money-market funds, of each fund's latest MUFAP "
                             "30-day annualised return in the week."),
    "income": ("Income", "d30", "Income funds — median 30-day annualised return",
               "Median, across income funds, of each fund's latest MUFAP 30-day annualised return in the week."),
    "equity": ("Equity", "d365", "Equity funds — median 1-year return",
               "Median, across conventional equity funds, of each fund's latest MUFAP 365-day return in the week."),
    "shariah_equity": ("Shariah Compliant Equity", "d365", "Shariah equity funds — median 1-year return",
                       "Median, across Shariah-compliant equity funds, of each fund's latest MUFAP 365-day return."),
}
for slug, (cat, col, name, method) in _FUND_CATEGORIES.items():
    key = f"funds:{cat}:{col}"
    SPECS.append(Spec(f"funds.category.{slug}.{'yield_30d' if col == 'd30' else 'return_1y'}", name,
                      "funds", "percent", "weekly", method, [key],
                      (lambda k: lambda i: weekly_median(i[k]))(key), (-90, 300)))

_CITIES_FROM_DB = "cost_of_living.%"  # expanded at run time: one YoY series per city index


def city_specs(index_ids: list[str]) -> list[Spec]:
    out = []
    for sid in sorted(index_ids):
        city = sid.split(".", 1)[1]
        label = "National" if city == "national" else city.replace("_", " ").title()
        out.append(Spec(f"prices.essentials_inflation.{city}", f"Essential-goods inflation (YoY) — {label}",
                        "prices", "percent", "weekly",
                        f"Year-on-year % change of the weekly cost-of-living index for {label} ({sid}: the "
                        f"unweighted mean of PBS SPI essential-item price relatives), vs the week 52 weeks earlier.",
                        [sid], (lambda s: lambda i: yoy_weekly(i[s]))(sid), (-60, 200)))
    return out


# ---- job --------------------------------------------------------------------------------

def load_inputs(keys: set[str]) -> dict[str, Points]:
    out: dict[str, Points] = {}
    for key in keys:
        if key.startswith("funds:"):
            _, cat, col = key.split(":")
            if col not in {"ytd", "mtd", "d30", "d90", "d365"}:
                raise ValueError(f"unknown fund_returns column {col}")
            rows = db.query(
                f"SELECT r.fund_id, r.obs_date, r.{col} AS v FROM fund_returns r JOIN funds f USING (fund_id) "
                f"WHERE f.category = %s AND r.{col} IS NOT NULL", (cat,))
            out[key] = [(r["fund_id"], r["obs_date"], float(r["v"])) for r in rows]  # type: ignore[misc]
            continue
        sid, _, dim = key.partition("|")
        if dim:
            k, v = dim.split("=")
            rows = db.query("SELECT obs_date, value FROM observations WHERE series_id = %s AND value IS NOT NULL "
                            "AND dims ->> %s = %s ORDER BY obs_date", (sid, k, v))
        else:
            rows = db.query("SELECT obs_date, value FROM observations WHERE series_id = %s AND value IS NOT NULL "
                            "AND dims = '{}'::jsonb ORDER BY obs_date", (sid,))
        out[key] = [(r["obs_date"], float(r["value"])) for r in rows]
    return out


class DeriveIndicatorsJob(IngestionJob):
    name = "derive_indicators"
    source = "PakDataHub"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            cities = [r["id"] for r in db.query(
                "SELECT id FROM series WHERE id LIKE %s AND is_active", (_CITIES_FROM_DB,))]
            specs = SPECS + city_specs(cities)
            inputs = load_inputs({k for s in specs for k in s.inputs})
            total, made, skipped = 0, 0, []
            for spec in specs:
                pts = [(d, round(v, 6)) for d, v in spec.compute(inputs)
                       if spec.bounds[0] <= v <= spec.bounds[1]]
                if not pts:
                    skipped.append(spec.id)
                    continue
                self._ensure_series(spec)
                total += self._upsert(spec.id, pts)
                made += 1
            status = "success" if total else "no_new_data"
            self._finish(run_id, status, total, None, None)
            return {"status": status, "rows": total, "series": made, "skipped": skipped}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            raise

    def _ensure_series(self, s: Spec) -> None:
        derivation = {"method": s.method, "inputs": [k.split("|")[0] for k in s.inputs], "version": s.version}
        db.execute(
            """INSERT INTO series (id, module, name, description, unit, frequency, source, tier, is_active,
                                   min_value, max_value, derivation)
               VALUES (%s, %s, %s, %s, %s, %s, 'PakDataHub', 'basic', true, %s, %s, %s::jsonb)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, description = EXCLUDED.description,
                   module = EXCLUDED.module, unit = EXCLUDED.unit, frequency = EXCLUDED.frequency,
                   source = 'PakDataHub', derivation = EXCLUDED.derivation, is_active = true""",
            (s.id, s.module, s.name, f"{s.method} Derived by PakDataHub from official series.",
             s.unit, s.frequency, s.bounds[0], s.bounds[1], json.dumps(derivation)),
        )

    def _upsert(self, sid: str, pts: Points) -> int:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                       VALUES (%s, %s, %s, '{}'::jsonb, false, now())
                       ON CONFLICT (series_id, obs_date, dims) DO UPDATE SET value = EXCLUDED.value
                       WHERE observations.value IS DISTINCT FROM EXCLUDED.value""",
                    [(sid, d, v) for d, v in pts],
                )
                # a point the formula no longer yields (input deleted/quarantined) goes too
                cur.execute("DELETE FROM observations WHERE series_id = %s AND NOT (obs_date = ANY(%s))",
                            (sid, [d for d, _ in pts]))
                cur.execute(
                    "UPDATE series SET first_date = (SELECT min(obs_date) FROM observations WHERE series_id=%s), "
                    "last_date = (SELECT max(obs_date) FROM observations WHERE series_id=%s) WHERE id=%s",
                    (sid, sid, sid),
                )
        return len(pts)
