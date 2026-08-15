from __future__ import annotations

from fastapi import APIRouter, Response

from app.services import cache, metrics, series_repo

router = APIRouter(prefix="/v1", tags=["status"])


@router.get("/status")
def status(response: Response):
    """Public, no-auth freshness. Doubles as a trust/marketing page and the
    primary ops dashboard.

    `modules` covers the generic series store; `datasets` covers the dedicated
    tables (funds, securities, trade, auctions) which the series view cannot see
    — together they account for the whole product. `jobs` reports run health.

    Cached for 60s: the aggregation is heavy and identical for every caller, and
    it's fetched on the home, coverage and status pages plus the nav on every page.
    """
    response.headers["Cache-Control"] = "public, max-age=60, s-maxage=60"

    def build() -> dict:
        return {
            "success": True,
            "modules": series_repo.module_status(),
            "datasets": series_repo.dataset_status(),
            "jobs": series_repo.job_status(),
            "ops": metrics.snapshot(),
        }

    return cache.cached_json("status", 60, build)
