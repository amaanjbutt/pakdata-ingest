from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services import search as site_search

router = APIRouter(prefix="/v1", tags=["search"])


@router.get("/search")
def search(
    q: str = Query(..., min_length=1, description="Search text; every word has to match"),
    module: str | None = Query(None, description="Restrict series results to one module"),
    kinds: str = Query("series", description="'series' (default), 'all', or a comma list of: "
                                             + ", ".join(site_search.KINDS)),
    limit: int = Query(20, ge=1, le=50),
):
    """Public ranked search — powers the site search (⌘K, /coverage, dashboard). No key needed.

    By default only series are returned (the long-standing behaviour client scripts rely on).
    `kinds=all` adds trade products and partner countries, districts, power plants, mandi crops
    and markets, mutual funds, AMCs and debt securities, each with `kind`, `title`, `subtitle`
    and the site `url`. Exact and prefix matches come first.
    """
    want = site_search.KINDS if kinds.strip() == "all" else tuple(k.strip() for k in kinds.split(",") if k.strip())
    bad = [k for k in want if k not in site_search.KINDS]
    if bad:
        raise HTTPException(status_code=422, detail=f"unknown kinds: {', '.join(bad)}")
    if want == ("series",):
        rows = site_search.search_series(q.strip(), module=module, limit=limit)
        for r in rows:
            r.pop("rank", None)
        return {"success": True, "data": rows, "count": len(rows)}
    rows = site_search.search_all(q.strip(), kinds=want, module=module, limit=limit)
    for r in rows:
        r.pop("rank", None)
    return {"success": True, "data": rows, "count": len(rows)}
