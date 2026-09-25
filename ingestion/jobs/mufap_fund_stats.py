"""mufap_fund_stats — fund payouts (dividends) and expense ratios (MUFAP).

Two server-rendered tables from the same page family, both keyed by FundID:

  Payouts        ?tab=4  Sector, AMC, Fund, Category, Inception,
                         Payout (Per Unit), Ex-NAV, Payout Date
  Expense ratios ?tab=5  Sector, AMC, Fund, Category, Inception, NAV,
                         TER MTD %, TER YTD %, MF %, S&M %, Validity Date

Payouts are events (keyed by payout_date); expense ratios are a daily snapshot
(keyed by validity date). Written to `fund_payouts` / `fund_expenses`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from app import db
from ingestion import alerting, storage
from ingestion.framework import IngestionJob

# The payouts report is date-ranged: with datefrom/datetill it returns every
# payout event in the window (not just the latest per fund). We fetch a rolling
# recent window each run so ongoing capture stays deep; deep history is seeded
# once by scripts/backfill_payouts.py.
PAYOUTS_BASE = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=4"
# Runs daily, so 45 days of overlap is plenty (deep history: scripts/backfill_payouts.py).
# Was 150: daily-dividend funds make that window thousands of rows, and the job
# started blowing the 300s GHA per-job timeout (2026-09-19 -> 09-24, payouts+TER stale).
PAYOUTS_WINDOW_DAYS = 45
EXPENSES_URL = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=5"
_FUNDID_RE = re.compile(r"FundID=(\d+)")


# TER above this (%) is a new-fund artifact (fixed costs / tiny or near-zero
# assets, or an early-fiscal-year annualization), not a real ongoing expense
# ratio — nulled rather than published. Real open-end fund TERs top out ~14.5%,
# so 15 is a safe ceiling: it drops the artifacts (a govt-securities plan at 17%,
# pension funds at 78-88%, a Rs-billions MTD glitch) without touching legit funds.
_MAX_TER_PCT = 15.0


def payouts_url(datefrom: date, datetill: date) -> str:
    return (f"{PAYOUTS_BASE}&AMCId=null&fundId=null"
            f"&datefrom={datefrom.isoformat()}&datetill={datetill.isoformat()}")


@dataclass
class PayoutRow:
    fund_id: int
    payout_date: date | None
    per_unit: float | None
    ex_nav: float | None


@dataclass
class ExpenseRow:
    fund_id: int
    obs_date: date | None
    ter_mtd: float | None
    ter_ytd: float | None
    mf: float | None
    sm: float | None


def _num(text: str) -> float | None:
    t = (text or "").strip().replace(",", "").rstrip("%")
    if not t or t in {"-", "--", "N/A", "n/a"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _date(text: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def _find_table(html: str, *required_headers: str):
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all("table"):
        heads = [c.get_text(strip=True).lower() for c in t.find_all("th")]
        if all(any(req in h for h in heads) for req in required_headers):
            return t
    return None


def _rows_with_fundid(table, min_cells: int):
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < min_cells:
            continue
        link = tr.find("a", href=_FUNDID_RE)
        if not link:
            continue
        yield (int(_FUNDID_RE.search(link["href"]).group(1)),
               [td.get_text(" ", strip=True) for td in tds])


def parse_payouts_html(html: str) -> list[PayoutRow]:
    """Parse the payouts table. Pure — no DB/network."""
    t = _find_table(html, "payout", "ex-nav")
    if t is None:
        raise ValueError("payouts table not found")
    out = [
        PayoutRow(fund_id=fid, per_unit=_num(c[5]), ex_nav=_num(c[6]),
                  payout_date=_date(c[7]))
        for fid, c in _rows_with_fundid(t, 8)
    ]
    if not out:
        raise ValueError("no payout rows parsed")
    return out


def parse_expenses_html(html: str) -> list[ExpenseRow]:
    """Parse the expense-ratio table. Pure — no DB/network."""
    t = _find_table(html, "ter mtd", "ter ytd")
    if t is None:
        raise ValueError("expense ratio table not found")
    out = [
        ExpenseRow(fund_id=fid, ter_mtd=_num(c[6]), ter_ytd=_num(c[7]),
                   mf=_num(c[8]), sm=_num(c[9]), obs_date=_date(c[10]))
        for fid, c in _rows_with_fundid(t, 11)
    ]
    if not out:
        raise ValueError("no expense rows parsed")
    return out


class MufapFundStatsJob(IngestionJob):
    name = "mufap_fund_stats"
    source = "MUFAP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            today = date.today()
            # Expenses first (one small page) so a slow/blocked payouts report can
            # never cost us the TER update; payouts failing leaves the run `partial`.
            exp_html = self.http_get(EXPENSES_URL)
            raw = storage.archive(self.source, self.name, today, "expenses.html", exp_html)
            expenses = parse_expenses_html(exp_html.decode("utf-8", errors="replace"))
            n_exp = self._upsert_expenses(expenses)

            n_pay, pay_err = 0, None
            try:
                pay_url = payouts_url(today - timedelta(days=PAYOUTS_WINDOW_DAYS), today)
                pay_html = self.http_get(pay_url)
                storage.archive(self.source, self.name, today, "payouts.html", pay_html)
                payouts = parse_payouts_html(pay_html.decode("utf-8", errors="replace"))
                n_pay = self._upsert_payouts(payouts)
            except Exception as exc:  # noqa: BLE001 - keep the expenses we already stored
                pay_err = f"payouts: {exc}"

            status = "partial" if pay_err else "success"
            self._finish(run_id, status, n_pay + n_exp, pay_err, raw)
            if pay_err:
                alerting.alert_unless_403(f"{self.name}: payouts failed", pay_err)
            return {"status": status, "rows": n_pay + n_exp,
                    "payouts": n_pay, "expenses": n_exp}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            alerting.alert_unless_403(f"{self.name}: job failed", str(exc))
            raise

    def _known_fund_ids(self, cur, ids: list[int]) -> set[int]:
        """Only rows for funds we already track (FK safety)."""
        cur.execute("SELECT fund_id FROM funds WHERE fund_id = ANY(%s)", (ids,))
        return {r[0] for r in cur.fetchall()}

    def _upsert_payouts(self, rows: list[PayoutRow]) -> int:
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                known = self._known_fund_ids(cur, [r.fund_id for r in rows])
                params = [(r.fund_id, r.payout_date, r.per_unit, r.ex_nav)
                          for r in rows if r.payout_date is not None and r.fund_id in known]
                # One pipelined batch, not a round trip per row: this job writes over an
                # SSH tunnel from GitHub Actions, where per-row RTT dominated the runtime.
                cur.executemany(
                    """
                    INSERT INTO fund_payouts (fund_id, payout_date, per_unit, ex_nav, revised_at)
                    VALUES (%s,%s,%s,%s, now())
                    ON CONFLICT (fund_id, payout_date) DO UPDATE SET
                        per_unit=EXCLUDED.per_unit, ex_nav=EXCLUDED.ex_nav, revised_at=now()
                    """,
                    params,
                )
                n = len(params)
        return n

    def _upsert_expenses(self, rows: list[ExpenseRow]) -> int:
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                known = self._known_fund_ids(cur, [r.fund_id for r in rows])
                params: list[tuple] = []
                for r in rows:
                    if r.obs_date is None or r.fund_id not in known:
                        continue
                    # A brand-new fund with tiny assets has an astronomical
                    # annualized TER (fixed costs / near-zero NAV) — real but
                    # meaningless. Null TERs above a sane cap so we never publish
                    # a "59% expense ratio"; keep the row for its MF/S&M.
                    ter_mtd = r.ter_mtd if (r.ter_mtd is None or r.ter_mtd <= _MAX_TER_PCT) else None
                    ter_ytd = r.ter_ytd if (r.ter_ytd is None or r.ter_ytd <= _MAX_TER_PCT) else None
                    # Skip rows with no usable expense data at all (0 = falsy).
                    if not any((ter_mtd, ter_ytd, r.mf, r.sm)):
                        continue
                    params.append((r.fund_id, r.obs_date, ter_mtd, ter_ytd, r.mf, r.sm))
                cur.executemany(
                    """
                    INSERT INTO fund_expenses (fund_id, obs_date, ter_mtd, ter_ytd, mf, sm, revised_at)
                    VALUES (%s,%s,%s,%s,%s,%s, now())
                    ON CONFLICT (fund_id, obs_date) DO UPDATE SET
                        ter_mtd=EXCLUDED.ter_mtd, ter_ytd=EXCLUDED.ter_ytd,
                        mf=EXCLUDED.mf, sm=EXCLUDED.sm, revised_at=now()
                    """,
                    params,
                )
                n = len(params)
        return n
