"""Debt-security endpoints (MUFAP per-instrument daily pricing).

    GET /v1/securities?type=pib_floating|gis_sukuk&q=
    GET /v1/securities/{code}/prices?from=&to=
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import Caller, authenticate, history_floor
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/securities", tags=["securities"])
MAX_LIMIT = 10000
_TYPES = {"pib_floating", "gis_sukuk", "tfc_sukuk"}


def _fnum(v):
    return float(v) if v is not None else None


@router.get("")
def list_securities(
    type: str | None = Query(None, description="pib_floating | gis_sukuk | tfc_sukuk"),
    q: str | None = Query(None, description="Code or issuer-name filter"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    caller: Caller = Depends(authenticate),
):
    if type is not None and type not in _TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {sorted(_TYPES)}")

    def build() -> dict:
        rows = series_repo.list_securities(security_type=type, q=q, limit=limit)
        return {
            "success": True,
            "data": [
                {"code": r["code"], "type": r["security_type"], "name": r.get("name"),
                 "rating_category": r.get("rating_category"), "sbp_code": r["sbp_code"],
                 "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                 "latest_price": {"date": r["price_date"], "price": _fnum(r["price"]),
                                  "net_change": _fnum(r["net_change"])} if r["price_date"] else None}
                for r in rows
            ],
            "count": len(rows),
        }

    return cache.cached_json(f"securities:{type}:{q}:{limit}", 300, build)


@router.get("/trades")
def debt_trades(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    issue: str | None = Query(None, description="Issuer / instrument name filter"),
    limit: int = Query(500, ge=1, le=MAX_LIMIT),
    caller: Caller = Depends(authenticate),
):
    """Trade-level secondary-market debt transactions (MUFAP daily trading)."""
    def build() -> dict:
        rows = series_repo.debt_trades(date_from=date_from, date_to=date_to, issue=issue, limit=limit)
        return {
            "success": True,
            "data": [
                {"trade_date": r["trade_date"], "bats": r["bats"], "issue_name": r["issue_name"],
                 "issue_date": r["issue_date"], "maturity_date": r["maturity_date"],
                 "listed": r["listed"], "face_value": _fnum(r["face_value"]),
                 "volume": _fnum(r["volume"]), "value_mn": _fnum(r["value_mn"]),
                 "price_pct": _fnum(r["price_pct"])}
                for r in rows
            ],
            "count": len(rows),
        }

    return cache.cached_json(f"trades:{date_from}:{date_to}:{issue}:{limit}", 300, build)


@router.get("/{code:path}/prices")
def security_prices(
    code: str,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    if not series_repo.get_security(code):
        raise HTTPException(status_code=404, detail="unknown security")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.security_prices(code, date_from=date_from, date_to=date_to,
                                       limit=limit, sort=sort)
    return {
        "success": True, "code": code,
        "data": [{"date": r["obs_date"], "price": _fnum(r["price"]),
                  "net_change": _fnum(r["net_change"])} for r in rows],
        "count": len(rows),
    }
