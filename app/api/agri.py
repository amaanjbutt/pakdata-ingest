"""Daily wholesale (mandi) prices — Punjab AMIS and Sindh AMIS — the `agri_prices` store.

    GET /v1/agri/markets?province=                market list (public)
    GET /v1/agri/commodities?province=            commodity list (public)
    GET /v1/agri/prices?commodity=&market=&from=&to=   daily min/max/FQP (keyed)
    GET /v1/agri/latest?commodity=&market=&crop=  latest price per market and/or commodity (keyed)

`commodity` and `market` take an AMIS id or a slug (`onion`, `lahore`). Punjab prices are Rs
per 100 kg, Sindh's Rs per kg (bananas per dozen) — always per the commodity's `unit`. Sindh
markets and commodities carry `province: "Sindh"`, ids above 900000 and slugs `sindh_…`
(`sindh_karachi`), and start in late August 2026 (one market rate per day, stored as `fqp`). Every row
also carries `fqp_per_kg`. `crop` (a Punjab commodity slug) selects every commodity of that crop in both
provinces — `crop=onion` = Punjab's onion + Sindh's graded onions. FQP = fair-quality price, the
representative price; min/max are the day's range (from Oct 2026 for most history —
the 2007+ backfill carries FQP only).
"""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app import db
from app.security import Caller, authenticate, history_floor
from app.services import cache
from ingestion.agri_names import SLUG_RENAMES

router = APIRouter(prefix="/v1/agri", tags=["agri"])
MAX_LIMIT = 10000


def _resolve(table: str, key: str, value: str) -> dict:
    col = "market_id" if table == "agri_markets" else "commodity_id"
    v = value.strip()
    if table == "agri_commodities":
        v = SLUG_RENAMES.get(v.lower(), v)  # slugs we corrected (cocunut -> coconut) keep working
    row = (db.query_one(f"SELECT * FROM {table} WHERE {col} = %s", (int(v),)) if v.isdigit()
           else db.query_one(f"SELECT * FROM {table} WHERE slug = %s OR lower(name) = lower(%s) "
                             "OR lower(display_name) = lower(%s) "
                             "ORDER BY (slug = %s) DESC, (province = 'Punjab') DESC LIMIT 1", (v, v, v, v)))
    if not row:
        raise HTTPException(status_code=404, detail=f"unknown {key}: {value}")
    return row


_PROVINCES = "^(Punjab|Sindh|punjab|sindh)$"
SOURCES = {"Punjab": "AMIS Punjab (Directorate of Agriculture, Economics & Marketing)",
           "Sindh": "Sindh Agricultural Market Intelligence (Sindh Agriculture Department)"}


@router.get("/markets")
def markets(province: str | None = Query(None, pattern=_PROVINCES, description="Punjab or Sindh; omit for both")):
    rows = cache.cached_json("agri:markets:v3", 3600, lambda: db.query(
        # per-market index lookups (idx_agri_prices_market_date) — a GROUP BY over the
        # 14M-row table took ~3 s on the 1-vCPU box
        """SELECT m.market_id, coalesce(m.display_name, m.name) AS name, m.name AS name_raw, m.slug,
                  m.is_major, m.province, coalesce(m.region, m.province) AS region, m.district,
                  (SELECT min(obs_date) FROM agri_prices p WHERE p.market_id = m.market_id) AS first_date,
                  (SELECT max(obs_date) FROM agri_prices p WHERE p.market_id = m.market_id) AS last_date
           FROM agri_markets m ORDER BY m.province, m.is_major DESC, m.name"""))
    if province:
        rows = [r for r in rows if r["province"] == province.title()]
    return {"success": True, "data": rows, "count": len(rows)}


@router.get("/commodities")
def commodities(province: str | None = Query(None, pattern=_PROVINCES, description="Punjab or Sindh; omit for both")):
    rows = cache.cached_json("agri:commodities:v3", 3600, lambda: db.query(
        "SELECT commodity_id, coalesce(display_name, name) AS name, name AS name_raw, urdu_name, slug, "
        "coalesce(crop_key, slug) AS crop, category, unit, province FROM agri_commodities "
        "ORDER BY province, category NULLS LAST, name"))
    if province:
        rows = [r for r in rows if r["province"] == province.title()]
    return {"success": True, "data": rows, "count": len(rows)}


def _per_kg(v, unit):
    if v is None:
        return None
    return round(float(v) / 100, 2) if unit == "pkr_per_100kg" else float(v) if unit == "pkr_per_kg" else None


def _out(rows: list[dict], fmt: str, extra: dict):
    data = [{"date": r["obs_date"].isoformat(), "market": r["market"], "market_id": r["market_id"],
             "market_slug": r["market_slug"], "region": r["region"],
             "commodity": r["commodity"], "commodity_id": r["commodity_id"],
             "commodity_slug": r["commodity_slug"], "province": r["province"],
             "min": float(r["min_price"]) if r["min_price"] is not None else None,
             "max": float(r["max_price"]) if r["max_price"] is not None else None,
             "fqp": float(r["fqp"]) if r["fqp"] is not None else None,
             "quantity": float(r["quantity"]) if r["quantity"] is not None else None,
             "unit": r["unit"], "fqp_per_kg": _per_kg(r["fqp"], r["unit"])} for r in rows]
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date", "market", "commodity", "min", "max", "fqp", "quantity", "unit"])
        for d in data:
            w.writerow([d["date"], d["market"], d["commodity"], d["min"], d["max"], d["fqp"], d["quantity"], d["unit"]])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return {"success": True, **extra, "data": data, "count": len(data)}


_SELECT = """SELECT p.obs_date, p.market_id, coalesce(m.display_name, m.name) AS market, m.slug AS market_slug,
                    coalesce(m.region, m.province) AS region, p.commodity_id,
                    coalesce(c.display_name, c.name) AS commodity, c.slug AS commodity_slug, c.province,
                    p.min_price, p.max_price, p.fqp, p.quantity, c.unit
             FROM agri_prices p JOIN agri_markets m USING (market_id) JOIN agri_commodities c USING (commodity_id)"""


@router.get("/prices")
def prices(
    commodity: str = Query(..., description="AMIS commodity id or slug, e.g. onion"),
    market: str | None = Query(None, description="AMIS market id or slug; omit for every market"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    format: str = Query("json", pattern="^(json|csv)$"),
    caller: Caller = Depends(authenticate),
):
    c = _resolve("agri_commodities", "commodity", commodity)
    clauses, params = ["p.commodity_id = %(c)s"], {"c": c["commodity_id"], "limit": limit}
    m = None
    if market:
        m = _resolve("agri_markets", "market", market)
        clauses.append("p.market_id = %(m)s")
        params["m"] = m["market_id"]
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    if date_from:
        clauses.append("p.obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("p.obs_date <= %(to)s")
        params["to"] = date_to
    order = "ASC" if sort == "asc" else "DESC"
    rows = db.query(f"{_SELECT} WHERE {' AND '.join(clauses)} ORDER BY p.obs_date {order}, m.name LIMIT %(limit)s",
                    params)
    return _out(rows, format, {"commodity": {k: c.get(k) for k in ("commodity_id", "name", "slug", "category", "unit",
                                                                   "province")},
                               "market": ({k: m.get(k) for k in ("market_id", "name", "slug", "province")} if m else None),
                               "source": SOURCES.get(c.get("province") or "Punjab", SOURCES["Punjab"])})


@router.get("/latest")
def latest(
    commodity: str | None = Query(None, description="AMIS commodity id or slug: its latest price in every market"),
    market: str | None = Query(None, description="AMIS market id or slug: every commodity's latest price there"),
    crop: str | None = Query(None, description="A crop (Punjab commodity slug, e.g. onion): every commodity "
                                               "of that crop in both provinces"),
    province: str | None = Query(None, pattern=_PROVINCES, description="With crop: Punjab or Sindh only"),
    format: str = Query("json", pattern="^(json|csv)$"),
    caller: Caller = Depends(authenticate),
):
    if not commodity and not market and not crop:
        raise HTTPException(status_code=422, detail="pass commodity, market, crop, or a combination")
    c = _resolve("agri_commodities", "commodity", commodity) if commodity else None
    m = _resolve("agri_markets", "market", market) if market else None
    # latest row per (commodity, market) within the last 30 days, for the given filter(s)
    where, params = [], {}
    crop_ids: list[int] = []
    if crop and not c:
        sql = "SELECT commodity_id FROM agri_commodities WHERE coalesce(crop_key, slug) = %s"
        crop_slug = crop.strip().lower()
        args: tuple = (SLUG_RENAMES.get(crop_slug, crop_slug),)
        if province:
            sql += " AND province = %s"
            args += (province.title(),)
        crop_ids = [r["commodity_id"] for r in db.query(sql, args)]
        if not crop_ids:
            raise HTTPException(status_code=404, detail=f"unknown crop: {crop}")
        where.append("commodity_id = ANY(%(cs)s)")
        params["cs"] = crop_ids
    if c:
        where.append("commodity_id = %(c)s")
        params["c"] = c["commodity_id"]
    if m:
        where.append("market_id = %(m)s")
        params["m"] = m["market_id"]
    cond = " AND ".join(where)
    key = (f"agri:latest:v2:{c['commodity_id'] if c else '-'}:{m['market_id'] if m else '-'}:"
           f"{','.join(map(str, crop_ids)) or '-'}")
    rows = cache.cached_json(key, 900, lambda: db.query(
        f"""{_SELECT} WHERE (p.commodity_id, p.market_id, p.obs_date) IN (
                SELECT commodity_id, market_id, max(obs_date) FROM agri_prices
                WHERE {cond} AND obs_date > current_date - 30 GROUP BY commodity_id, market_id)
            ORDER BY m.name, c.name""", params))
    rows = [{**r, "obs_date": date.fromisoformat(r["obs_date"]) if isinstance(r["obs_date"], str) else r["obs_date"]}
            for r in rows]
    extra: dict = {}
    if c:
        extra["commodity"] = {k: c.get(k) for k in ("commodity_id", "name", "slug", "category", "unit", "province")}
    if m:
        extra["market"] = {k: m.get(k) for k in ("market_id", "name", "slug", "province")}
    if crop_ids:
        extra["crop"] = args[0]
        extra["sources"] = {p: SOURCES[p] for p in sorted({r["province"] for r in rows})}
    ref = c or m
    extra["source"] = SOURCES.get((ref.get("province") if ref else None) or "Punjab", SOURCES["Punjab"])
    return _out(rows, format, extra)
