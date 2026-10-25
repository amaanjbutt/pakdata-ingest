from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.services import cache, series_repo

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])

# SeriesMeta's public fields. We project to plain JSON-safe dicts once (in the
# cached producer) and return them via JSONResponse — bypassing FastAPI's
# response_model re-validation, which on a large module (up to 500 rows) added
# seconds of pydantic work per request on the 1-vCPU box.
_META_FIELDS = (
    "id", "module", "name", "description", "unit", "frequency", "source",
    "source_url", "dimensions", "first_date", "last_date", "tier",
    # Set when this series is a label-duplicate of another (identical name +
    # description): points at the canonical id so the page can emit rel=canonical
    # + noindex and stop competing with its twins in search.
    "canonical_id",
)
_CACHE_HEADERS = {"Cache-Control": "public, max-age=300, s-maxage=300"}


def _meta_dict(row: dict) -> dict:
    d = {k: row.get(k) for k in _META_FIELDS}
    for k in ("first_date", "last_date"):
        v = d.get(k)
        if v is not None and not isinstance(v, str):
            d[k] = v.isoformat()
    d["dimensions"] = d.get("dimensions") or {}
    d["tier"] = d.get("tier") or "basic"
    # The derived layer (source 'PakDataHub') carries its formula: {"method", "inputs", "version"}.
    d["derived"] = row.get("derivation")
    return d


@router.get("")
def get_catalog(
    module: str | None = Query(None),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Full-text filter over id/name/description"),
    limit: int = Query(500, ge=1, le=5000, description="Max rows returned"),
    offset: int = Query(0, ge=0, description="Row offset for pagination"),
    canonical_only: bool = Query(
        False, description="Exclude label-duplicate series (for hub/index listings)"
    ),
) -> JSONResponse:
    def build() -> dict:
        rows = series_repo.list_series(
            module=module, source=source, q=q, limit=limit, offset=offset,
            canonical_only=canonical_only,
        )
        total = series_repo.count_series(
            module=module, source=source, q=q, canonical_only=canonical_only
        )
        return {"data": [_meta_dict(r) for r in rows], "count": total}

    cached = cache.cached_json(
        f"catalog:{module}:{source}:{q}:{limit}:{offset}:{canonical_only}", 300, build
    )
    return JSONResponse(
        {"success": True, "data": cached["data"],
         "pagination": {"next_cursor": None, "count": cached["count"]}},
        headers=_CACHE_HEADERS,
    )


@router.get("/{series_id}")
def get_series_meta(series_id: str) -> JSONResponse:
    cached = cache.cached_json(
        f"catalog_meta:{series_id}",
        300,
        lambda: (lambda r: _meta_dict(r) if r else None)(series_repo.get_series(series_id)),
    )
    if not cached:
        raise HTTPException(status_code=404, detail="unknown series")
    return JSONResponse(cached, headers=_CACHE_HEADERS)
