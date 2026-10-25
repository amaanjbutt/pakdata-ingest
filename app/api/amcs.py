"""AMC (asset-management company) aggregate profiles.

Rolls the per-fund store up to the AMC entity — fund count, category mix, average
expense ratio (TER), rating distribution, and oldest fund — the "entity-
relationship" view a competitor comparison flagged as missing. Derived from
`funds` + the latest per-fund expenses/ratings.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app import db
from app.security import Caller, authenticate
from app.services import cache

router = APIRouter(prefix="/v1/amcs", tags=["funds"])

# Latest expense ratio and rating per fund (funds report these as time series).
_LATEST = """
WITH latest_ter AS (
  SELECT DISTINCT ON (fund_id) fund_id, ter_ytd
  FROM fund_expenses ORDER BY fund_id, obs_date DESC
),
latest_rating AS (
  SELECT DISTINCT ON (fund_id) fund_id, rating
  FROM fund_returns WHERE rating IS NOT NULL AND rating <> ''
  ORDER BY fund_id, obs_date DESC
)
"""


@router.get("")
def list_amcs(caller: Caller = Depends(authenticate)):
    """Every AMC with fund count, average TER, category mix, and oldest fund."""
    def build() -> dict:
        rows = db.query(
            _LATEST + """
            SELECT f.amc,
                   count(*)                                     AS fund_count,
                   round(avg(lt.ter_ytd)::numeric, 2)           AS avg_ter_ytd,
                   min(f.inception_date)                        AS oldest_inception,
                   array_agg(DISTINCT f.category)
                     FILTER (WHERE f.category IS NOT NULL)      AS categories
            FROM funds f
            LEFT JOIN latest_ter lt ON lt.fund_id = f.fund_id
            WHERE f.amc IS NOT NULL AND f.amc <> ''
            GROUP BY f.amc
            ORDER BY fund_count DESC, f.amc
            """
        )
        data = [
            {"amc": r["amc"], "fund_count": int(r["fund_count"]),
             "avg_ter_ytd": float(r["avg_ter_ytd"]) if r["avg_ter_ytd"] is not None else None,
             "oldest_inception": r["oldest_inception"], "categories": r["categories"] or []}
            for r in rows
        ]
        return {"success": True, "data": data, "count": len(data)}

    return cache.cached_json("amcs:list", 3600, build)


@router.get("/{amc}")
def get_amc(amc: str, caller: Caller = Depends(authenticate)):
    """One AMC: aggregates, rating distribution, and its funds."""
    if not db.query_one("SELECT 1 FROM funds WHERE lower(amc) = lower(%(a)s) LIMIT 1", {"a": amc}):
        raise HTTPException(status_code=404, detail="unknown AMC")

    def build() -> dict:
        agg = db.query_one(
            _LATEST + """
            SELECT count(*) AS fund_count,
                   round(avg(lt.ter_ytd)::numeric, 2) AS avg_ter_ytd,
                   min(f.inception_date) AS oldest_inception,
                   array_agg(DISTINCT f.category) FILTER (WHERE f.category IS NOT NULL) AS categories
            FROM funds f LEFT JOIN latest_ter lt ON lt.fund_id = f.fund_id
            WHERE lower(f.amc) = lower(%(a)s)
            """, {"a": amc},
        )
        ratings = db.query(
            _LATEST + """
            SELECT lr.rating, count(*) AS n
            FROM funds f JOIN latest_rating lr ON lr.fund_id = f.fund_id
            WHERE lower(f.amc) = lower(%(a)s)
            GROUP BY lr.rating ORDER BY n DESC
            """, {"a": amc},
        )
        funds = db.query(
            "SELECT fund_id, name, category, inception_date FROM funds "
            "WHERE lower(amc) = lower(%(a)s) ORDER BY name", {"a": amc},
        )
        return {
            "success": True,
            "amc": amc,
            "fund_count": int(agg["fund_count"]),
            "avg_ter_ytd": float(agg["avg_ter_ytd"]) if agg["avg_ter_ytd"] is not None else None,
            "oldest_inception": agg["oldest_inception"],
            "categories": agg["categories"] or [],
            "rating_distribution": {r["rating"]: int(r["n"]) for r in ratings},
            "funds": [{"fund_id": r["fund_id"], "name": r["name"], "category": r["category"],
                       "inception_date": r["inception_date"]} for r in funds],
        }

    return cache.cached_json(f"amc:{amc.lower()}", 3600, build)
