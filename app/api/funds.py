"""Mutual fund endpoints (MUFAP daily NAVs).

    GET /v1/funds?amc=&category=&sector=&q=   list funds + latest NAV
    GET /v1/funds/{fund_id}                    fund metadata + latest NAV
    GET /v1/funds/{fund_id}/nav?from=&to=      NAV history
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import Caller, authenticate, history_floor
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/funds", tags=["funds"])
MAX_LIMIT = 10000


def _fnum(v):
    return float(v) if v is not None else None


@router.get("")
def list_funds(
    amc: str | None = Query(None),
    category: str | None = Query(None),
    sector: str | None = Query(None),
    q: str | None = Query(None, description="Fund name filter"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    caller: Caller = Depends(authenticate),
):
    def build() -> dict:
        rows = series_repo.list_funds(amc=amc, category=category, sector=sector, q=q, limit=limit)
        data = [
            {
                "fund_id": r["fund_id"], "name": r["name"], "amc": r["amc"],
                "sector": r["sector"], "category": r["category"],
                "inception_date": r["inception_date"], "trustee": r["trustee"],
                "latest_nav": {
                    "date": r["nav_date"], "nav": _fnum(r["nav"]),
                    "offer": _fnum(r["offer"]), "repurchase": _fnum(r["repurchase"]),
                } if r["nav_date"] else None,
            }
            for r in rows
        ]
        return {"success": True, "data": data, "count": len(data)}

    key = f"funds:{amc}:{category}:{sector}:{q}:{limit}"
    return cache.cached_json(key, 300, build)


@router.get("/{fund_id}")
def get_fund(fund_id: int, caller: Caller = Depends(authenticate)):
    f = series_repo.get_fund(fund_id)
    if not f:
        raise HTTPException(status_code=404, detail="unknown fund")
    latest = series_repo.fund_navs(fund_id, limit=1)
    return {
        "success": True,
        "fund": {
            "fund_id": f["fund_id"], "name": f["name"], "amc": f["amc"],
            "sector": f["sector"], "category": f["category"],
            "inception_date": f["inception_date"], "trustee": f["trustee"],
        },
        "latest_nav": {
            "date": latest[0]["obs_date"], "nav": _fnum(latest[0]["nav"]),
            "offer": _fnum(latest[0]["offer"]), "repurchase": _fnum(latest[0]["repurchase"]),
        } if latest else None,
    }


@router.get("/{fund_id}/returns")
def fund_returns(
    fund_id: int,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    """Trailing-return performance history (YTD/MTD/1D..365D) for a fund."""
    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.fund_returns(fund_id, date_from=date_from, date_to=date_to,
                                    limit=limit, sort=sort)
    return {
        "success": True,
        "fund_id": fund_id,
        "data": [
            {"date": r["obs_date"], "rating": r["rating"], "benchmark": r["benchmark"],
             "ytd": _fnum(r["ytd"]), "mtd": _fnum(r["mtd"]), "d1": _fnum(r["d1"]),
             "d15": _fnum(r["d15"]), "d30": _fnum(r["d30"]), "d90": _fnum(r["d90"]),
             "d180": _fnum(r["d180"]), "d270": _fnum(r["d270"]), "d365": _fnum(r["d365"])}
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/{fund_id}/payouts")
def fund_payouts(
    fund_id: int,
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    caller: Caller = Depends(authenticate),
):
    """Dividend/payout history for a fund."""
    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    rows = series_repo.fund_payouts(fund_id, limit=limit)
    return {
        "success": True, "fund_id": fund_id,
        "data": [{"payout_date": r["payout_date"], "per_unit": _fnum(r["per_unit"]),
                  "ex_nav": _fnum(r["ex_nav"])} for r in rows],
        "count": len(rows),
    }


@router.get("/{fund_id}/expenses")
def fund_expenses(
    fund_id: int,
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    caller: Caller = Depends(authenticate),
):
    """Expense-ratio history (TER MTD/YTD, management fee, selling & marketing)."""
    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    rows = series_repo.fund_expenses(fund_id, limit=limit)
    return {
        "success": True, "fund_id": fund_id,
        "data": [{"date": r["obs_date"], "ter_mtd": _fnum(r["ter_mtd"]),
                  "ter_ytd": _fnum(r["ter_ytd"]), "management_fee": _fnum(r["mf"]),
                  "selling_marketing": _fnum(r["sm"])} for r in rows],
        "count": len(rows),
    }


@router.get("/{fund_id}/portfolio")
def fund_portfolio(fund_id: int, caller: Caller = Depends(authenticate)):
    """Latest asset-allocation / portfolio composition (% by asset class)."""
    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    rows = series_repo.fund_portfolio(fund_id)
    return {
        "success": True, "fund_id": fund_id,
        "as_of": rows[0]["as_of_date"] if rows else None,
        "data": [{"asset_class": r["asset_class"], "percent": _fnum(r["percent"])} for r in rows],
        "count": len(rows),
    }


@router.get("/{fund_id}/nav")
def fund_nav_history(
    fund_id: int,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.fund_navs(fund_id, date_from=date_from, date_to=date_to,
                                 limit=limit, sort=sort)
    return {
        "success": True,
        "fund_id": fund_id,
        "data": [
            {"date": r["obs_date"], "nav": _fnum(r["nav"]),
             "offer": _fnum(r["offer"]), "repurchase": _fnum(r["repurchase"])}
            for r in rows
        ],
        "count": len(rows),
    }
