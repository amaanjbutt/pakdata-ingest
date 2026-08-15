"""PBS external trade endpoints.

    GET /v1/trade?flow=export|import&commodity=&group=&from=&to=
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import Caller, authenticate, history_floor
from app.services import series_repo

router = APIRouter(prefix="/v1/trade", tags=["trade"])
MAX_LIMIT = 10000
_FLOWS = {"export", "import"}


def _fnum(v):
    return float(v) if v is not None else None


@router.get("")
def trade(
    flow: str | None = Query(None, description="export | import"),
    commodity: str | None = Query(None, description="Commodity name filter"),
    group: str | None = Query(None, description="Commodity group filter"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    """Monthly export/import by commodity. Values: PKR million, USD thousand."""
    if flow is not None and flow not in _FLOWS:
        raise HTTPException(status_code=422, detail="flow must be 'export' or 'import'")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.trade_flows(flow=flow, commodity=commodity, group=group,
                                   date_from=date_from, date_to=date_to,
                                   limit=limit, sort=sort)
    return {
        "success": True,
        "data": [
            {"flow": r["flow"], "commodity": r["commodity"],
             "group": r["commodity_group"], "date": r["obs_date"], "unit": r["unit"],
             "quantity": _fnum(r["quantity"]),
             "value_pkr_mn": _fnum(r["value_pkr_mn"]),
             "value_usd_th": _fnum(r["value_usd_th"])}
            for r in rows
        ],
        "count": len(rows),
    }
