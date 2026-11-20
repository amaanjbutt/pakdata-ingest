"""Full-history charts for a signed-in person on pakdatahub.com.

The public page (what a visitor who isn't signed in sees, and what search engines see) charts
the last 5 years. Any signed-in account sees the whole history as a picture: the last 5 years at
published precision (daily series one point a week) and older years as monthly or annual
averages — never the published daily or monthly figures, which come from a download or the API.

Session auth only (an API key is not accepted), metered per account (50 series a day on the
free plan; reopening a series the same day is free) and per address. Past the meter the call
answers 429 `chart_limit` and the page keeps its 5-year chart.

    GET /v1/chart/series/{series_id}?dims=&transform=
    GET /v1/chart/fund/{fund_id}
    GET /v1/chart/security/{code}
    GET /v1/chart/agri?commodity=&market=
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.security import (Caller, authenticate_session, download_quota, take_chart, visitor_ip)
from app.services import cache, datasets

router = APIRouter(prefix="/v1/chart", tags=["website"], include_in_schema=False)

TABLE_ROWS = 24  # observations table on the page for a signed-in person


def _parse_dims(dims: str | None) -> dict[str, str] | None:
    if not dims:
        return None
    out: dict[str, str] = {}
    for pair in dims.split(","):
        if ":" not in pair:
            raise HTTPException(status_code=422, detail=f"bad dims token: {pair!r}")
        k, v = pair.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _iso(v):
    return v.isoformat() if isinstance(v, date) else v


def _payload(ds: datasets.Dataset) -> dict:
    chart = datasets.chart_points(ds.rows, ds.frequency)
    return {
        "subject": ds.subject,
        "meta": {"name": ds.name, "unit": ds.unit, "frequency": ds.frequency, "source": ds.source},
        "first_date": _iso(ds.rows[0]["date"]) if ds.rows else None,
        "last_date": _iso(ds.rows[-1]["date"]) if ds.rows else None,
        "count": len(ds.rows),
        "recent_from": _iso(chart["recent_from"]),
        "older": chart["older"],
        "points": [{**p, "date": _iso(p["date"])} for p in chart["points"]],
        "table": [{**r, "date": _iso(r["date"])} for r in datasets.recent_rows(ds, TABLE_ROWS)],
    }


def _serve(request: Request, caller: Caller, key: str, loader) -> dict:
    if not take_chart(caller, key, visitor_ip(request)):
        exc = HTTPException(
            status_code=429,
            detail=(f"full-history charts are limited to {caller.limits.charts_per_day} series a day on this "
                    "plan — the chart shows the last 5 years; downloads and the API give every value"),
        )
        exc.error_code = "chart_limit"
        raise exc
    # The same picture for everyone who asks today: computed once an hour per series.
    body = cache.cached_json(f"chart:v1:{key}", 3600, lambda: _payload(loader()))
    return {"success": True, **body, "plan": caller.plan, "downloads": download_quota(caller)}


@router.get("/series/{series_id}")
def series_chart(request: Request, series_id: str, dims: str | None = Query(None),
                 transform: str | None = Query(None, pattern="^(yoy|mom|pct_change|3ma|index)$"),
                 caller: Caller = Depends(authenticate_session)):
    want = _parse_dims(dims)
    key = f"series:{series_id}:{datasets.dims_json(want)}:{transform or ''}"
    return _serve(request, caller, key, lambda: datasets.load_series(
        series_id, want, one_group=True, transform=transform))


@router.get("/fund/{fund_id}")
def fund_chart(request: Request, fund_id: int, caller: Caller = Depends(authenticate_session)):
    return _serve(request, caller, f"fund:{fund_id}", lambda: datasets.load_fund(fund_id))


@router.get("/security/{code:path}")
def security_chart(request: Request, code: str, caller: Caller = Depends(authenticate_session)):
    return _serve(request, caller, f"security:{code}", lambda: datasets.load_security(code))


@router.get("/agri")
def agri_chart(request: Request, commodity: str = Query(...), market: str = Query(...),
               caller: Caller = Depends(authenticate_session)):
    key = f"agri:{commodity.strip().lower()}:{market.strip().lower()}"
    return _serve(request, caller, key, lambda: datasets.load_agri(commodity, market))
