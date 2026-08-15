"""Commodity price endpoints (Phase 3) over the PBS weekly SPI data.

Ergonomic wrappers over the generic series store for the `commodity.*` series,
which carry a `{"city": <key>}` dimension. `city=national` is the derived
unweighted mean across reporting cities.

    GET /v1/commodities?item=&from=&to=        national average time series
    GET /v1/commodities/by-city?item=&city=    per-city time series
    GET /v1/commodities/items                   item enum (public)
    GET /v1/commodities/cities                  city enum (public)
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models import Observation, Pagination, ResponseMeta, SeriesResponse
from app.security import Caller, authenticate, enforce_tier, history_floor
from app.services import series_repo
from ingestion.commodities_config import load_cities, load_items

router = APIRouter(prefix="/v1/commodities", tags=["commodities"])

ITEM_PREFIX = "commodity."
MAX_LIMIT = 10000


def _series_id(item: str) -> str:
    """Accept either 'wheat_flour' or 'commodity.wheat_flour'."""
    item = item.strip()
    return item if item.startswith(ITEM_PREFIX) else f"{ITEM_PREFIX}{item}"


def _observations(rows: list[dict]) -> list[Observation]:
    return [
        Observation(
            date=r["obs_date"],
            value=float(r["value"]) if r["value"] is not None else None,
            dims=r.get("dims") or {},
        )
        for r in rows
    ]


def _meta(row: dict) -> ResponseMeta:
    return ResponseMeta(
        name=row["name"], unit=row.get("unit"), frequency=row["frequency"],
        source=row["source"], last_updated=row.get("last_date"),
    )


def _price_series(
    item: str, city: str, date_from: date | None, date_to: date | None,
    limit: int, sort: str, caller: Caller,
) -> SeriesResponse:
    series_id = _series_id(item)
    meta_row = series_repo.get_series(series_id)
    if not meta_row:
        raise HTTPException(status_code=404, detail=f"unknown commodity: {item}")
    enforce_tier(caller, meta_row)
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.get_observations(
        series_id, date_from=date_from, date_to=date_to,
        dims={"city": city}, limit=limit, sort=sort,
    )
    return SeriesResponse(
        series=series_id,
        meta=_meta(meta_row),
        data=_observations(rows),
        pagination=Pagination(next_cursor=None, count=len(rows)),
    )


@router.get("/items")
def items():
    """Public enum of available commodity items."""
    return {
        "success": True,
        "data": [{"id": it.id, "name": it.name, "unit": it.unit} for it in load_items()],
    }


@router.get("/cities")
def cities():
    """Public enum of the SPI city panel (plus the derived 'national')."""
    keys = sorted(set(load_cities().values()))
    return {"success": True, "data": keys}


@router.get("")
def national(
    item: str = Query(..., description="Commodity id, e.g. wheat_flour or commodity.wheat_flour"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    """National (derived) average retail price time series for an item."""
    return _price_series(item, "national", date_from, date_to, limit, sort, caller)


@router.get("/by-city")
def by_city(
    item: str = Query(..., description="Commodity id, e.g. wheat_flour"),
    city: str = Query(..., description="City key, e.g. karachi (see /v1/commodities/cities)"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    """Per-city retail price time series for an item."""
    city = city.strip().lower()
    if city not in set(load_cities().values()):
        raise HTTPException(status_code=404, detail=f"unknown city: {city}")
    return _price_series(item, city, date_from, date_to, limit, sort, caller)
