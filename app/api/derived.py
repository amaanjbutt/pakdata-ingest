"""The derived layer in one call — every PakDataHub-derived series with its formula and latest value.

    GET /v1/derived      (keyed)

The same data is in `/v1/catalog?source=PakDataHub` plus `/v1/series/{id}/latest` per series; this exists
so the derived hub page is one request rather than 56. (The website's Worker may make 50 requests per
page on Cloudflare's free plan: past that, the last series came back empty and the page couldn't be
stored at the edge, so it was rendered again on every visit — 2026-11-14.)
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import db
from app.api.catalog import _meta_dict
from app.security import Caller, authenticate, history_floor
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/derived", tags=["derived"])

# Newest point per series through the (series_id, obs_date DESC) index: one row each, no scan.
_LATEST = """
    SELECT s.id AS series_id, l.obs_date, l.value
    FROM unnest(%(ids)s::text[]) AS s(id)
    LEFT JOIN LATERAL (
        SELECT obs_date, value FROM observations o
        WHERE o.series_id = s.id
        ORDER BY obs_date DESC, dims::text DESC
        LIMIT 1
    ) l ON true
"""


def _build() -> dict:
    rows = series_repo.list_series(source="PakDataHub", limit=1000)
    latest = {
        r["series_id"]: r
        for r in db.query(_LATEST, {"ids": [r["id"] for r in rows]})
        if r["obs_date"] is not None
    }
    data = []
    for r in rows:
        rec = _meta_dict(r)
        p = latest.get(r["id"])
        rec["latest"] = (
            {"date": p["obs_date"].isoformat(), "value": float(p["value"]) if p["value"] is not None else None}
            if p else None
        )
        data.append(rec)
    return {"data": data}


@router.get("")
def derived_hub(caller: Caller = Depends(authenticate)) -> JSONResponse:
    """Every derived series (catalog record incl. `derived` = method, inputs, version) with `latest`
    = {date, value} of its newest observation, or null. One request."""
    payload = cache.cached_json("derived:hub", 300, _build)
    floor = history_floor(caller)
    data = []
    for rec in payload["data"]:
        hidden = (rec.get("tier") == "pro" and not caller.limits.allow_pro) or (
            floor is not None and rec.get("latest") and date.fromisoformat(rec["latest"]["date"]) < floor
        )
        data.append({**rec, "latest": None if hidden else rec.get("latest")})
    return JSONResponse({"success": True, "data": data, "pagination": {"next_cursor": None, "count": len(data)}})
