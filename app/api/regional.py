"""District profiles in one call — the `regional` module's series for one place.

    GET /v1/regional/districts            every district with its headline figures (public)
    GET /v1/regional/district/{name}      every regional series' rows for one district, plus the
                                          province's survey figures for comparison (keyed)

The same data is in `/v1/series/{id}?dims=district:…`; these exist so a district page is one
request rather than one per indicator.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.security import Caller, authenticate
from app.services import cache

router = APIRouter(prefix="/v1/regional", tags=["regional"])

_HEADLINE = """
    WITH latest AS (
        SELECT DISTINCT ON (o.series_id, o.dims) o.series_id, o.obs_date, o.value, o.dims
        FROM observations o
        WHERE o.series_id = ANY(%(ids)s) AND o.dims ? 'district' AND NOT o.dims ? 'subdistrict'
        ORDER BY o.series_id, o.dims, o.obs_date DESC)
    SELECT series_id, obs_date, value, dims FROM latest
"""
_INDEX_SERIES = {
    "regional.census.population": {"locality": "total", "sex": "all"},
    "regional.census.growth_rate": {"locality": "total"},
    "regional.census.literacy_rate": {"locality": "total", "sex": "all"},
    "regional.census.urban_share": {},
    "regional.pslm.adult_literacy": {"locality": "total", "sex": "all"},
}
_FIELDS = {"regional.census.population": "population", "regional.census.growth_rate": "growth",
           "regional.census.literacy_rate": "literacy", "regional.census.urban_share": "urban",
           "regional.pslm.adult_literacy": "adult_literacy"}


def _f(v):
    return float(v) if v is not None else None


def _districts() -> list[dict]:
    rows = db.query(_HEADLINE, {"ids": list(_INDEX_SERIES)})
    out: dict[tuple, dict] = {}
    pop_date: dict[tuple, str] = {}
    for r in rows:
        want = _INDEX_SERIES[r["series_id"]]
        dims = r["dims"] or {}
        if any(dims.get(k) != v for k, v in want.items()):
            continue
        key = (dims.get("province"), dims["district"])
        d = out.setdefault(key, {"province": key[0], "district": key[1]})
        d[_FIELDS[r["series_id"]]] = _f(r["value"])
        if r["series_id"] == "regional.census.population":
            pop_date[key] = r["obs_date"].isoformat()
    # districts as drawn in 2023 (earlier-census names like Karachi stay in /v1/series)
    latest = max(pop_date.values(), default=None)
    return sorted((d | {"census_date": pop_date.get(k)} for k, d in out.items() if pop_date.get(k) == latest),
                  key=lambda d: -(d.get("population") or 0))


@router.get("/districts")
def districts():
    """Every district (2023 boundaries) with population, growth, literacy, urban share and adult
    literacy. Public."""
    rows = cache.cached_json("regional:districts:v1", 3600, _districts)
    return {"success": True, "data": rows, "count": len(rows)}


def _profile(name: str) -> dict | None:
    rows = db.query(
        """SELECT o.series_id, o.obs_date, o.value, o.dims FROM observations o
           JOIN series s ON s.id = o.series_id AND s.module = 'regional' AND s.is_active
           WHERE o.dims->>'district' = %(name)s AND NOT o.dims ? 'subdistrict'
           ORDER BY o.series_id, o.obs_date""",
        {"name": name})
    if not rows:
        return None
    province = (rows[0]["dims"] or {}).get("province")
    prov = db.query(
        """SELECT o.series_id, o.obs_date, o.value, o.dims FROM observations o
           WHERE o.series_id LIKE 'regional.pslm.%%' AND o.dims->>'province' = %(p)s AND NOT o.dims ? 'district'
           ORDER BY o.series_id, o.obs_date""",
        {"p": province})

    def pack(rs):
        return [{"series": r["series_id"], "date": r["obs_date"].isoformat(), "value": _f(r["value"]),
                 "dims": r["dims"] or {}} for r in rs]
    return {"district": name, "province": province, "rows": pack(rows), "province_rows": pack(prov)}


@router.get("/district/{name}")
def district(name: str, caller: Caller = Depends(authenticate)):
    """Every `regional` series' rows for one district (census history, PSLM, Economic and
    Agricultural Census), plus the province's PSLM figures."""
    data = cache.cached_json(f"regional:district:v1:{name.lower()}", 3600, lambda: _profile(name.strip()))
    if not data:
        raise HTTPException(status_code=404, detail=f"unknown district: {name}")
    return {"success": True, "data": data}
