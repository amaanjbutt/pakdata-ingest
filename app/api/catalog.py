from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.models import CatalogResponse, Pagination, SeriesMeta
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


def _to_meta(row: dict) -> SeriesMeta:
    return SeriesMeta(**row)


@router.get("", response_model=CatalogResponse)
def get_catalog(
    response: Response,
    module: str | None = Query(None),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Full-text filter over id/name/description"),
    limit: int = Query(500, ge=1, le=5000, description="Max rows returned"),
) -> CatalogResponse:
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"

    def build() -> dict:
        rows = series_repo.list_series(module=module, source=source, q=q, limit=limit)
        total = series_repo.count_series(module=module, source=source, q=q)
        return {"rows": rows, "total": total}

    cached = cache.cached_json(f"catalog:{module}:{source}:{q}:{limit}", 300, build)
    data = [_to_meta(r) for r in cached["rows"]]
    # `count` is the total matching the filter; `data` is capped at `limit`.
    return CatalogResponse(data=data, pagination=Pagination(next_cursor=None, count=cached["total"]))


@router.get("/{series_id}", response_model=SeriesMeta)
def get_series_meta(series_id: str, response: Response) -> SeriesMeta:
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=300"
    cached = cache.cached_json(
        f"catalog_meta:{series_id}", 300, lambda: series_repo.get_series(series_id)
    )
    if not cached:
        raise HTTPException(status_code=404, detail="unknown series")
    return _to_meta(cached)
