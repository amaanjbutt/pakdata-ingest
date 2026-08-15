from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app import db

router = APIRouter(prefix="/v1", tags=["calendar"])


@router.get("/calendar")
def calendar(
    next: int | None = Query(None, ge=1, le=100, description="Return the next N upcoming releases"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    module: str | None = Query(None),
):
    """Public release calendar — when each indicator is next expected to publish.

    `?next=N` returns the next N upcoming releases; otherwise a `from`/`to` window
    (defaulting to the next 30 days). No key needed.
    """
    clauses = ["1=1"]
    params: dict = {}
    if next:
        clauses.append("c.release_date >= CURRENT_DATE")
        params["limit"] = next
        limit_sql = "LIMIT %(limit)s"
    else:
        clauses.append("c.release_date >= COALESCE(%(from)s, CURRENT_DATE)")
        clauses.append("c.release_date <= COALESCE(%(to)s, CURRENT_DATE + 30)")
        params["from"] = date_from
        params["to"] = date_to
        limit_sql = ""
    if module:
        clauses.append("s.module = %(module)s")
        params["module"] = module

    rows = db.query(
        f"""
        SELECT c.release_date AS date, c.indicator, c.source, c.series_id, c.actual_value
        FROM release_calendar c
        LEFT JOIN series s ON s.id = c.series_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.release_date, c.indicator
        {limit_sql}
        """,
        params,
    )
    return {"success": True, "data": rows, "count": len(rows)}
