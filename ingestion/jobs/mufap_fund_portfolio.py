"""mufap_fund_portfolio — per-fund asset allocation / portfolio composition.

Source: POST https://www.mufap.com.pk/AMC/GetFundDetailbyAMCByDate {FundID, Date}
— the same endpoint that carries NAV history (Table1). Its **Table2** holds the
fund's latest monthly portfolio breakdown: the percent of assets in cash,
equities, PIBs, T-Bills, Ijara Sukuk, TFCs, placements, and so on.

One call per fund. Stored long-format in `fund_portfolio` (fund_id, as_of_date,
asset_class, percent). Monthly cadence — the source data is a monthly snapshot.
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime

from app import db
from ingestion import alerting, http_client
from ingestion.framework import IngestionJob

DETAIL_URL = "https://www.mufap.com.pk/AMC/GetFundDetailbyAMCByDate"

# Table2 percent field -> clean asset-class label. Non-zero allocations only.
ASSET_CLASSES: dict[str, str] = {
    "Cashpercent": "Cash",
    "PlacementsWithBanksandDFIsPercent": "Bank & DFI placements",
    "PlacementsWithNBFCsPercent": "NBFC placements",
    "ReverseReposAgainstGovernmentSecuritiesPercent": "Reverse repo (govt)",
    "ReverseReposAgainstAllOtherSecuritiesPercent": "Reverse repo (other)",
    "TFCsPercent": "TFCs & corporate sukuk",
    "GovernmentBackedORGuaranteedSecuritiesPercent": "Govt-backed securities",
    "StocksOREquitiesPercent": "Equities",
    "PIBsPercent": "PIBs",
    "TBillsPercent": "T-Bills",
    "IjarahSukuksPercent": "Ijara Sukuk",
    "CommercialpapersPercent": "Commercial paper",
    "CFSPercent": "CFS / MTS",
    "SpreadTransactionPercent": "Spread transactions",
    "OtherInvestAmountFundOfFundPercent": "Fund of funds",
    "OtherIncludingReceivablePercent": "Other & receivables",
    "LaibilitiesPercent": "Liabilities",
}


def _num(v) -> float | None:
    try:
        return float(v)
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
    Pure — no DB/network."""
    outer = json.loads(json_text)
    inner = json.loads(outer["data"]) if isinstance(outer.get("data"), str) else outer.get("data") or {}
    rows = inner.get("Table2") or []
    if not rows:
        return None, []
    r = rows[0]
    as_of = _date(r.get("Date"))
    out: list[tuple[str, float]] = []
    for field, label in ASSET_CLASSES.items():
        pct = _num(r.get(field))
        if pct is not None and abs(pct) > 0.0:
            out.append((label, pct))
    return as_of, out


class MufapFundPortfolioJob(IngestionJob):
    name = "mufap_fund_portfolio"
    source = "MUFAP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            fund_ids = [r["fund_id"] for r in db.query(
                "SELECT fund_id FROM funds WHERE is_active = true ORDER BY fund_id"
            )]
            total = 0
            funds_done = 0
            for fid in fund_ids:
                try:
                    resp = http_client.post(
                        DETAIL_URL,
                        json={"FundID": fid, "Date": date.today().strftime("%m/%d/%Y")},
                        timeout=60,
                    )
                    as_of, rows = parse_portfolio(resp.text)
                except Exception as exc:  # skip a bad fund, keep going
                    alerting.alert(f"{self.name}: fund fetch failed", f"fund {fid}: {exc}")
                    continue
                if as_of and rows:
                    total += self._upsert(fid, as_of, rows)
                    funds_done += 1
                time.sleep(0.3)  # be a good citizen
            self._finish(run_id, "success", total, None, None)
            return {"status": "success", "rows": total, "funds": funds_done}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            alerting.alert(f"{self.name}: job failed", str(exc))
            raise

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
