"""Fixed-income auction endpoints (Phase 4.4).

    GET /v1/fixed-income/auctions?type=tbill|pib|gis&from=&to=

Reads the dedicated `auctions` table. Headline cut-off yields are also available
as ordinary series (auction.<type>.<tenor>.cutoff_yield) via /v1/series.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.security import Caller, authenticate
from app.services import series_repo

router = APIRouter(prefix="/v1/fixed-income", tags=["fixed-income"])

_TYPES = {"tbill", "pib", "gis"}


@router.get("/auctions")
def auctions(
    type: str | None = Query(None, description="tbill | pib | gis"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(500, ge=1, le=5000),
    caller: Caller = Depends(authenticate),
):
    if type is not None and type not in _TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of {sorted(_TYPES)}")
    rows = series_repo.list_auctions(
        auction_type=type, date_from=date_from, date_to=date_to, limit=limit
    )
    data = [
        {
            "type": r["auction_type"],
            "tenor": r["tenor"],
            "auction_date": r["auction_date"],
            "settlement_date": r["settlement_date"],
            "cutoff_yield": float(r["cutoff_yield"]) if r["cutoff_yield"] is not None else None,
            "offered_amount": float(r["offered_amount"]) if r["offered_amount"] is not None else None,
            "accepted_amount": float(r["accepted_amount"]) if r["accepted_amount"] is not None else None,
            "bid_to_cover": float(r["bid_to_cover"]) if r["bid_to_cover"] is not None else None,
            "source": r["source"],
        }
        for r in rows
    ]
    return {"success": True, "data": data, "count": len(data)}
