from __future__ import annotations

import csv
import json
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app.models import (
    Observation,
    Pagination,
    ResponseMeta,
    SeriesResponse,
)
from app.security import Caller, authenticate, enforce_tier, history_floor
from app.services import cache, series_repo, transforms

router = APIRouter(prefix="/v1/series", tags=["series"])

MAX_LIMIT = 10000

# A transform compares each point with an EARLIER one (yoy: a year back, mom: a
# month back, pct_change/3ma: the previous 1-2 periods). If we only fetched the
# requested window, the oldest points of that window would have no partner and
# come back null (e.g. `?transform=yoy&limit=14` returned 12 nulls). So for these
# transforms fetch a lookback before the window, compute, then trim to the window.
# `index` rebases to the first point IN the window, so it must not look back.
_LOOKBACK_DAYS = {"yoy": 400, "mom": 40}
_PERIOD_DAYS = {
    "daily": 10, "weekly": 21, "monthly": 45, "quarterly": 120,
    "half-yearly": 200, "semiannual": 200, "annual": 400,
}
TRANSFORM_FETCH_CAP = 50000


def _transform_lookback(kind: str, frequency: str | None) -> timedelta | None:
    if kind == "index":
        return None
    if kind in _LOOKBACK_DAYS:
        return timedelta(days=_LOOKBACK_DAYS[kind])
    per = _PERIOD_DAYS.get((frequency or "").lower(), 400)
    return timedelta(days=per * (3 if kind == "3ma" else 1))


def _trim_window(rows_asc: list[dict], date_from: date | None, limit: int, sort: str) -> list[dict]:
    """Rows arrive ascending from apply_transform. Keep the requested window, then
    apply `limit` the same way the DB query would (desc -> the most recent N)."""
    if date_from is not None:
        rows_asc = [r for r in rows_asc if r["obs_date"] >= date_from]
    if sort == "desc":
        return list(reversed(rows_asc))[:limit]
    return rows_asc[:limit]


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


def _meta(row: dict, transform: str | None = None) -> ResponseMeta:
    unit = row.get("unit")
    if transform:
        unit = transforms.transformed_unit(transform, unit)
    return ResponseMeta(
        name=row["name"],
        unit=unit,
        frequency=row["frequency"],
        source=row["source"],
        last_updated=row.get("last_date"),
        transform=transform,
        derived=row.get("derivation"),
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


def _dims_cell(d: dict | None) -> str:
    """dims as compact JSON ({"side":"offer"}) — it was a Python repr ({'side': 'offer'})
    that spreadsheets and CSV parsers couldn't read back. Empty dims -> ""."""
    return json.dumps(d, separators=(",", ":"), sort_keys=True) if d else ""


def _csv_response(series_id: str, rows: list[dict]) -> PlainTextResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["series", "date", "value", "dims"])
    for r in rows:
        w.writerow([
            _csv_safe(series_id),
            _csv_safe(r["obs_date"]),
            _csv_safe(r["value"]),
            _csv_safe(_dims_cell(r.get("dims"))),
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
    transform: str | None = Query(
        None,
        description="Server-side transform: yoy | mom | pct_change | 3ma | index",
    ),
    vintage: date | None = Query(
        None,
        description="Revision vintage: values as known on this date (YYYY-MM-DD). Pro+.",
    ),
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
    if vintage is not None and caller.plan not in ("pro", "business"):
        # Revision vintages are a Pro/Business feature (pricing "Vintages" row).
        raise HTTPException(status_code=403, detail="vintages require the Pro plan")
    lookback = None
    if transform:
        try:
            transforms.validate(transform, meta_row["frequency"])
        except transforms.TransformError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        lookback = _transform_lookback(transform, meta_row["frequency"])
    # With a partner-needing transform: fetch everything from (from - lookback),
    # uncapped by `limit`, and trim after computing.
    fetch_from = date_from - lookback if (lookback and date_from) else date_from
    fetch_limit = TRANSFORM_FETCH_CAP if lookback else limit
    if vintage is not None:
        rows = series_repo.get_observations_asof(
            series_id,
            vintage,
            date_from=fetch_from,
            date_to=date_to,
            dims=_parse_dims(dims),
            limit=fetch_limit,
            sort=sort,
        )
    else:
        rows = series_repo.get_observations(
            series_id,
            date_from=fetch_from,
            date_to=date_to,
            dims=_parse_dims(dims),
            limit=fetch_limit,
            sort=sort,
        )
    if transform:
        try:
            rows = transforms.apply_transform(rows, transform, meta_row["frequency"])
        except transforms.TransformError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        # apply_transform returns ascending; trim to the window + honor direction.
        rows = _trim_window(rows, date_from, limit, sort)
    if format == "csv":
        return _csv_response(series_id, rows)
    meta = _meta(meta_row, transform)
    meta.notes = series_repo.get_notes(series_id, [r["obs_date"] for r in rows]) or None
    return SeriesResponse(
        series=series_id,
        meta=meta,
        data=_observations(rows),
        pagination=Pagination(next_cursor=None, count=len(rows)),
    )


@router.get("/{series_id}/revisions")
def get_series_revisions(
    series_id: str,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    dims: str | None = Query(None, description="Comma-separated key:value filters"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    changed_only: bool = Query(False, description="Only points that were ever revised"),
    caller: Caller = Depends(authenticate),
):
    """Revision history per point: first print, every superseded figure with the date
    it stopped being current (and where that print came from), and today's value.
    Pro/Business, like `?vintage=`."""
    meta_row = series_repo.get_series(series_id)
    if not meta_row:
        raise HTTPException(status_code=404, detail="unknown series")
    enforce_tier(caller, meta_row)
    if caller.plan not in ("pro", "business"):
        raise HTTPException(status_code=403, detail="revision history requires the Pro plan")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.get_revisions(
        series_id, date_from=date_from, date_to=date_to, dims=_parse_dims(dims),
        limit=limit, sort=sort, changed_only=changed_only,
    )
    revised = sum(1 for r in rows if r["revisions"])
    return {
        "success": True,
        "series": series_id,
        "meta": _meta(meta_row).model_dump(mode="json"),
        "data": [{**r, "date": r["date"].isoformat(),
                  "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
                  "revisions": [{**v, "until": str(v["until"])} for v in r["revisions"]]} for r in rows],
        "pagination": {"next_cursor": None, "count": len(rows), "revised_points": revised},
    }


@router.get("/{series_id}/latest")
def get_series_latest(
    series_id: str,
    dims: str | None = Query(None, description="Comma-separated key:value filters"),
    format: str = Query("json", pattern="^(json|csv)$"),
    caller: Caller = Depends(authenticate),
):
    meta_row = cache.cached_json(
        f"meta:{series_id}", 300, lambda: series_repo.get_series(series_id)
    )
    if not meta_row:
        raise HTTPException(status_code=404, detail="unknown series")
    enforce_tier(caller, meta_row)
    rows = cache.cached_json(f"latest:{series_id}", 60, lambda: series_repo.get_latest(series_id))
    # dims filter (e.g. side:offer) — /latest used to ignore it and return every side.
    want = _parse_dims(dims)
    if want:
        rows = [r for r in rows if all(str((r.get("dims") or {}).get(k)) == v for k, v in want.items())]
    if format == "csv":
        return _csv_response(series_id, rows)
    meta = _meta(meta_row)
    meta.notes = series_repo.get_notes(series_id, [r["obs_date"] for r in rows]) or None
    return SeriesResponse(
        series=series_id,
        meta=meta,
        data=_observations(rows),
        pagination=Pagination(next_cursor=None, count=len(rows)),
    )
