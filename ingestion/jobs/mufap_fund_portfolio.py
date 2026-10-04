"""mufap_fund_portfolio — per-fund asset allocation / portfolio composition.

Source: POST https://www.mufap.com.pk/AMC/GetFundDetailbyAMCByDate {FundID, Date}
— the same endpoint that carries NAV history (Table1). Its **Table2** holds the
fund's latest monthly portfolio breakdown: the percent of assets in cash,
equities, PIBs, T-Bills, Ijara Sukuk, TFCs, placements, and so on.

One call per fund. Stored long-format in `fund_portfolio` (fund_id, as_of_date,
asset_class, percent). Monthly cadence — the source data is a monthly snapshot.

Resumable: MUFAP's Cloudflare re-blocks a runner IP partway through ~540 calls,
so each run works stalest-first (funds not refreshed in REFRESH_DAYS), stops as
`partial` on a run budget or a streak of 403s, and the next good runner carries
on where it stopped. Without this, a blocked run spent ~15s per fund on 403
retries and was killed by the CI timeout while still marked 'running'.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime

from app import db
from ingestion import alerting, http_client
from ingestion.framework import IngestionJob

DETAIL_URL = "https://www.mufap.com.pk/AMC/GetFundDetailbyAMCByDate"
# A fund refreshed within this many days is skipped (weekly job, monthly data).
REFRESH_DAYS = 5
# Stop cleanly before the CI job timeout (45 min) kills the process.
RUN_BUDGET_SECONDS = int(os.getenv("MUFAP_PORTFOLIO_BUDGET_SECONDS", "1800"))
# This many 403s in a row = the IP has been blocked; stop rather than grind.
MAX_CONSECUTIVE_403 = 5

# Table2 **amount** field -> clean asset-class label. We compute each allocation
# as amount / Total * 100 rather than trusting MUFAP's `*Percent` fields, because:
#   (1) some classes (notably `Commodity` = gold) have an amount but NO percent
#       field, so a percent-only parser silently dropped a gold/commodity fund's
#       biggest holding (Meezan Gold showed 14% instead of 100%);
#   (2) when a fund's Total is 0 MUFAP's percent fields blow up into garbage
#       (e.g. -24,000,000%), which never nets to 100 — we skip those funds instead.
# `Laibilities` [sic] is stored negative in the source, so its sign carries through
# naturally and the allocation nets to ~100%.
ASSET_AMOUNT_FIELDS: dict[str, str] = {
    "Cash": "Cash",
    "PlacementsWithBanksandDFIs": "Bank & DFI placements",
    "PlacementsWithNBFCs": "NBFC placements",
    "ReverseReposAgainstGovernmentSecurities": "Reverse repo (govt)",
    "ReverseReposAgainstAllOtherSecurities": "Reverse repo (other)",
    "TFCs": "TFCs & corporate sukuk",
    "GovernmentBackedORGuaranteedSecurities": "Govt-backed securities",
    "StocksOREquities": "Equities",
    "PIBs": "PIBs",
    "TBills": "T-Bills",
    "IjarahSukuks": "Ijara Sukuk",
    "Commercialpapers": "Commercial paper",
    "CFS": "CFS / MTS",
    "SpreadTransaction": "Spread transactions",
    "OtherInvestAmountFundOfFund": "Fund of funds",
    "OtherIncludingReceivable": "Other & receivables",
    "Commodity": "Commodity / Gold",   # amount only — no percent field in Table2
    "Laibilities": "Liabilities",
}

# A single allocation line beyond this (abs) can only be residual garbage from a
# near-zero Total that slipped the Total>0 gate; drop the line, keep the fund.
_MAX_LINE_PCT = 150.0

# A whole fund's allocation must reconcile to ~100% of net assets to be trusted;
# outside this band the source data is broken/incomplete, so we store nothing.
_MIN_SUM_PCT = 90.0
_MAX_SUM_PCT = 112.0


def _num(v) -> float | None:
    # MUFAP sometimes returns percents as strings with a "%" sign or thousands
    # separators (e.g. "85%", "1,234"). float() throws on those, silently
    # dropping the fund's biggest holding — strip them first.
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _date(v) -> date | None:
    if not v:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(v)[:19], fmt).date()
        except ValueError:
            continue
    return None


def parse_portfolio(json_text: str) -> tuple[date | None, list[tuple[str, float]]]:
    """(as_of_date, [(asset_class, percent)]) from a GetFundDetailbyAMCByDate body.
    Percents are computed from each asset's amount divided by the fund's net Total,
    so classes without a `*Percent` field (e.g. Commodity/gold) are included and a
    fund's allocation nets to ~100%. Pure — no DB/network."""
    outer = json.loads(json_text)
    inner = json.loads(outer["data"]) if isinstance(outer.get("data"), str) else outer.get("data") or {}
    rows = inner.get("Table2") or []
    if not rows:
        return None, []
    r = rows[0]
    as_of = _date(r.get("Date"))
    total = _num(r.get("Total"))
    # Total is net assets (Rs mn); if it's zero/missing the source is broken for
    # this fund (its percent fields blow up), so we can't compute an allocation.
    if total is None or abs(total) < 1e-6:
        return as_of, []
    out: list[tuple[str, float]] = []
    for field, label in ASSET_AMOUNT_FIELDS.items():
        amount = _num(r.get(field))
        if amount is None or amount == 0.0:
            continue
        pct = amount / total * 100.0
        if abs(pct) > _MAX_LINE_PCT:
            continue
        out.append((label, round(pct, 4)))
    # Reconciliation gate: a valid allocation must net to ~100% of net assets.
    # A handful of funds have broken source data (a huge negative fund-of-funds
    # line, a near-zero Total, or only a fraction of holdings reported) that never
    # reconciles — storing that partial breakdown would mislead, so drop it (the
    # fund shows no allocation rather than a wrong one).
    if out and not (_MIN_SUM_PCT <= sum(p for _, p in out) <= _MAX_SUM_PCT):
        return as_of, []
    return as_of, out


class MufapFundPortfolioJob(IngestionJob):
    name = "mufap_fund_portfolio"
    source = "MUFAP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            fund_ids = self._funds_to_refresh(backfill)
            if not fund_ids:
                self._finish(run_id, "no_new_data", 0, None, None)
                return {"status": "no_new_data", "rows": 0, "funds": 0}
            deadline = time.monotonic() + RUN_BUDGET_SECONDS
            total = 0
            funds_done = 0
            streak_403 = 0
            stop_reason = None
            for fid in fund_ids:
                if time.monotonic() > deadline:
                    stop_reason = "run budget reached"
                    break
                if streak_403 >= MAX_CONSECUTIVE_403:
                    stop_reason = f"{streak_403} consecutive 403s (IP blocked)"
                    break
                try:
                    resp = http_client.post(
                        DETAIL_URL,
                        json={"FundID": fid, "Date": date.today().strftime("%m/%d/%Y")},
                        timeout=60,
                    )
                    as_of, rows = parse_portfolio(resp.text)
                except Exception as exc:  # skip a bad fund, keep going
                    streak_403 = streak_403 + 1 if "403" in str(exc) else 0
                    alerting.alert_unless_403(f"{self.name}: fund fetch failed", f"fund {fid}: {exc}")
                    continue
                streak_403 = 0
                if as_of and rows:
                    total += self._upsert(fid, as_of, rows)
                    funds_done += 1
                time.sleep(0.3)  # be a good citizen
            if funds_done == 0 and streak_403 >= MAX_CONSECUTIVE_403:
                # Blocked from the start: a 403 failure (alert-suppressed), not a partial.
                raise RuntimeError(f"HTTP Error 403: stopped before any fund — {stop_reason}")
            status = "partial" if stop_reason else "success"
            msg = f"{stop_reason}; {funds_done}/{len(fund_ids)} funds" if stop_reason else None
            self._finish(run_id, status, total, msg, None)
            return {"status": status, "rows": total, "funds": funds_done,
                    "queued": len(fund_ids)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            alerting.alert_unless_403(f"{self.name}: job failed", str(exc))
            raise

    def _funds_to_refresh(self, everything: bool = False) -> list[int]:
        """Active funds, stalest portfolio first; funds refreshed within
        REFRESH_DAYS are skipped (unless `everything`). Funds that never yield a
        valid allocation (the reconciliation gate drops them) go last, so a good
        IP is spent on funds that will actually store data."""
        rows = db.query(
            """
            SELECT f.fund_id, p.last_rev
            FROM funds f
            LEFT JOIN (SELECT fund_id, max(revised_at) AS last_rev
                       FROM fund_portfolio GROUP BY fund_id) p USING (fund_id)
            WHERE f.is_active = true
              AND (%(all)s OR p.last_rev IS NULL
                   OR p.last_rev < now() - make_interval(days => %(days)s))
            ORDER BY p.last_rev ASC NULLS LAST, f.fund_id
            """,
            {"all": everything, "days": REFRESH_DAYS},
        )
        return [r["fund_id"] for r in rows]

    def _upsert(self, fund_id: int, as_of: date, rows: list[tuple[str, float]]) -> int:
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO fund_portfolio (fund_id, as_of_date, asset_class, percent, revised_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (fund_id, as_of_date, asset_class) DO UPDATE SET
                        percent = EXCLUDED.percent, revised_at = now()
                    """,
                    [(fund_id, as_of, cls, pct) for cls, pct in rows],
                )
        return len(rows)
