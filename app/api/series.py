from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.models import (
    Observation,
    Pagination,
    ResponseMeta,
    SeriesResponse,
)
from app.security import Caller, authenticate, enforce_tier, history_floor
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/series", tags=["series"])

MAX_LIMIT = 10000


def _parse_dims(dims: str | None) -> dict[str, str] | None:
    """Parse repeated `key:value` dim filters, e.g. dims=side:offer."""
    if not dims:
        return None
    out: dict[str, str] = {}
    for pair in dims.split(","):
        if ":" not in pair:
            raise HTTPException(status_code=422, detail=f"bad dims token: {pair!r}")
        k, v = pair.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def _meta(row: dict) -> ResponseMeta:
    return ResponseMeta(
        name=row["name"],
        unit=row.get("unit"),
        frequency=row["frequency"],
        source=row["source"],
        last_updated=row.get("last_date"),
    )


def _observations(rows: list[dict]) -> list[Observation]:
    return [
        Observation(
            date=r["obs_date"],
            value=float(r["value"]) if r["value"] is not None else None,
            dims=r.get("dims") or {},
        )
        for r in rows
    ]


def _csv_safe(v) -> str:
    """Neutralize CSV formula injection: cells opened in Excel/Sheets that begin
    with = + - @ (or tab/CR) execute as formulas. Prefix a single quote."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def _csv_response(series_id: str, rows: list[dict]) -> PlainTextResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["series", "date", "value", "dims"])
    for r in rows:
        w.writerow([
            _csv_safe(series_id),
            _csv_safe(r["obs_date"]),
            _csv_safe(r["value"]),
            _csv_safe(r.get("dims") or {}),
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")


@router.get("/{series_id}")
def get_series_data(
    series_id: str,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    dims: str | None = Query(None, description="Comma-separated key:value filters"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    format: str = Query("json", pattern="^(json|csv)$"),
    caller: Caller = Depends(authenticate),
):
    """Observations for a series.

    Pagination: results are capped at `limit` (max %d) and `next_cursor` is
    always null in this MVP. To page through a long history, window with the
    `from`/`to` date params rather than a cursor. (Keyset cursors are deferred:
    a series can carry several observations per date across `dims`, so a naive
    date cursor would split or drop rows at a page boundary.)
    """ % MAX_LIMIT
    meta_row = series_repo.get_series(series_id)
    if not meta_row:
        raise HTTPException(status_code=404, detail="unknown series")
    enforce_tier(caller, meta_row)
    # History-depth gating: Basic callers are clamped to the last N years.
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.get_observations(
        series_id,
        date_from=date_from,
        date_to=date_to,
        dims=_parse_dims(dims),
        limit=limit,
        sort=sort,
    )
    if format == "csv":
        return _csv_response(series_id, rows)
    return SeriesResponse(
        series=series_id,
        meta=_meta(meta_row),
        data=_observations(rows),
        pagination=Pagination(next_cursor=None, count=len(rows)),
    )


@router.get("/{series_id}/latest")
def get_series_latest(series_id: str, caller: Caller = Depends(authenticate)):
    meta_row = cache.cached_json(
        f"meta:{series_id}", 300, lambda: series_repo.get_series(series_id)
    )
    if not meta_row:
        raise HTTPException(status_code=404, detail="unknown series")
    enforce_tier(caller, meta_row)
    rows = cache.cached_json(f"latest:{series_id}", 60, lambda: series_repo.get_latest(series_id))
    return SeriesResponse(
        series=series_id,
        meta=_meta(meta_row),
        data=_observations(rows),
        pagination=Pagination(next_cursor=None, count=len(rows)),
    )
