from __future__ import annotations

from fastapi import APIRouter, Query

from app.services import series_repo

router = APIRouter(prefix="/v1", tags=["search"])


@router.get("/search")
def search(
    q: str = Query(..., min_length=1, description="Search text over id, name, description"),
    module: str | None = Query(None),
    limit: int = Query(20, ge=1, le=50),
):
    """Public ranked catalog search — powers the ⌘K command palette. No key needed.

    Returns the most relevant active series first (exact id, then id-prefix, then
    name, then description matches).
    """
    rows = series_repo.search_series(q.strip(), module=module, limit=limit)
    return {"success": True, "data": rows, "count": len(rows)}
