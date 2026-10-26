"""PBS external trade endpoints.

    GET /v1/trade?flow=export|import&commodity=&group=&from=&to=     monthly, by commodity group
    GET /v1/trade/products?q=                                        product codes (public)
    GET /v1/trade/hs?flow=&code=&country=&from=&to=                  yearly, by product x country
    GET /v1/trade/partners?flow=&fy=&code=                           partners ranked for a year
    GET /v1/trade/partner-products?country=&flow=&fy=                a partner's products ranked
    GET /v1/trade/partner-history?country=&flow=                     a partner's yearly total
    GET /v1/trade/countries?flow=                                    partner list (public)
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app import db
from app.security import Caller, authenticate, history_floor
from app.services import cache, series_repo

router = APIRouter(prefix="/v1/trade", tags=["trade"])
MAX_LIMIT = 10000
_FLOWS = {"export", "import"}


def _fnum(v):
    return float(v) if v is not None else None


@router.get("")
def trade(
    flow: str | None = Query(None, description="export | import"),
    commodity: str | None = Query(None, description="Commodity name filter"),
    group: str | None = Query(None, description="Commodity group filter"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
    caller: Caller = Depends(authenticate),
):
    """Monthly export/import by commodity. Values: PKR million, USD thousand."""
    if flow is not None and flow not in _FLOWS:
        raise HTTPException(status_code=422, detail="flow must be 'export' or 'import'")
    floor = history_floor(caller)
    if floor is not None and (date_from is None or date_from < floor):
        date_from = floor
    rows = series_repo.trade_flows(flow=flow, commodity=commodity, group=group,
                                   date_from=date_from, date_to=date_to,
                                   limit=limit, sort=sort)
    return {
        "success": True,
        "data": [
            {"flow": r["flow"], "commodity": r["commodity"],
             "group": r["commodity_group"], "date": r["obs_date"], "unit": r["unit"],
             "quantity": _fnum(r["quantity"]),
             "value_pkr_mn": _fnum(r["value_pkr_mn"]),
             "value_usd_th": _fnum(r["value_usd_th"])}
            for r in rows
        ],
        "count": len(rows),
    }


# ---- Exports / imports by product code x partner country, fiscal years 1990-91 on (`trade_hs`) -------

_HS_FLOWS = "^(export|import|re-export|re-import)$"
_ALL = "All countries"
# Fiscal-year average PKR per USD: the mean of the twelve SBP monthly averages, July-June.
_FY_RATE = """fy_rate AS (
    SELECT (EXTRACT(YEAR FROM obs_date)::int + CASE WHEN EXTRACT(MONTH FROM obs_date) >= 7 THEN 1 ELSE 0 END) AS fy,
           avg(value) AS pkr_per_usd
    FROM observations WHERE series_id = 'fx.rate.avg.usd' GROUP BY 1)"""


def _fy_label(fy: int) -> str:
    return f"{fy - 1}-{str(fy)[2:]}"


def _fy_floor(caller: Caller) -> int | None:
    floor = history_floor(caller)
    return None if floor is None else floor.year + (1 if floor.month >= 7 else 0)


def _check_fy(caller: Caller, fy: int) -> None:
    floor = _fy_floor(caller)
    if floor and fy < floor:
        raise HTTPException(status_code=403, detail=f"your plan's history starts at FY{_fy_label(floor)}")


# trade_hs only changes when a year is loaded (which busts `cache:v1:trade:*`), and the ranking
# queries below aggregate tens of thousands of rows — cache them for hours, not seconds.
_TTL = 6 * 3600


def _countries(flow: str) -> list[dict]:
    return cache.cached_json(f"trade:hs_countries:{flow}:v1", 3600, lambda: db.query(
        "SELECT country, min(fy) AS first_fy, max(fy) AS last_fy FROM trade_hs "
        "WHERE flow = %(flow)s AND country <> %(all)s GROUP BY 1 ORDER BY 1", {"flow": flow, "all": _ALL}))


def _canon_country(flow: str, country: str) -> str:
    """The stored spelling of a partner (matched case-insensitively), so queries can compare
    `country = …` and use the country index — `lower(country) = …` scanned all 2.9M rows."""
    c = country.strip()
    if c.lower() == _ALL.lower():
        return _ALL
    return next((r["country"] for r in _countries(flow) if r["country"].lower() == c.lower()), c)


def _code_filter(code: str | None, params: dict) -> str:
    if not code:
        return ""
    c = code.strip().upper()
    if c.endswith("*"):
        params["code"] = c[:-1] + "%"
        return " AND t.code LIKE %(code)s"
    params["code"] = c
    return " AND t.code = %(code)s"


def _usd(v, rate):
    return round(float(v) / float(rate), 3) if v is not None and rate else None


@router.get("/products")
def hs_products(q: str | None = Query(None, description="Words to find in the product name or heading, or a code prefix"),
                classification: str | None = Query(None, pattern="^(HS|SITC)$"),
                limit: int = Query(50, ge=1, le=1000)):
    """Product codes in the customs record: 8-digit HS-based codes (FY2002-03 on) and 7-digit
    SITC-based codes (FY1990-91 to FY2001-02), with a readable `name` (the official HS/SITC text
    where the code is the official item, otherwise PBS's wording spelled out), the official
    parent `hs_heading`, and PBS's raw abbreviated text in `description`. Public."""
    rows = cache.cached_json("trade:hs_codes:v2", 3600, lambda: db.query(
        "SELECT code, classification, coalesce(display_name, description) AS name, short_name, "
        "hs_heading, description, coalesce(unit_clean, nullif(unit, '')) AS unit, first_fy, last_fy "
        "FROM trade_hs_codes WHERE classification <> 'total' ORDER BY code"))
    if classification:
        rows = [r for r in rows if r["classification"] == classification]
    if q:
        ql = q.strip().lower()
        words = ql.split()

        def hit(r) -> bool:
            if r["code"].startswith(ql.replace(".", "")):
                return True
            text = " ".join(x for x in (r["name"], r["short_name"], r["hs_heading"], r["description"]) if x).lower()
            return all(w in text for w in words)
        rows = [r for r in rows if hit(r)]
    return {"success": True, "data": rows[:limit], "count": min(len(rows), limit), "total": len(rows)}


@router.get("/hs")
def hs_flows(flow: str = Query("export", pattern=_HS_FLOWS),
             code: str | None = Query(None, description="Product code, or a prefix ending in * (e.g. 5205*)"),
             country: str | None = Query(None, description="Partner country; 'All countries' = the product total"),
             fy_from: int | None = Query(None, alias="from", ge=1990, le=2100, description="First fiscal year (end year)"),
             fy_to: int | None = Query(None, alias="to", ge=1990, le=2100),
             limit: int = Query(1000, ge=1, le=MAX_LIMIT),
             caller: Caller = Depends(authenticate)):
    """Yearly exports/imports for a product (or code prefix), by partner country. Value in Rs
    thousand as published, plus US$ thousand at the fiscal-year average exchange rate."""
    if not code and not country:
        raise HTTPException(status_code=422, detail="give a product code (or prefix*) or a country")
    params: dict = {"flow": flow, "limit": limit, "all": _ALL}
    where = "t.flow = %(flow)s" + _code_filter(code, params)
    if country:
        where += " AND t.country = %(country)s"
        params["country"] = _canon_country(flow, country)
    lo = max(fy_from or 0, _fy_floor(caller) or 0) or None
    if lo:
        where += " AND t.fy >= %(lo)s"
        params["lo"] = lo
    if fy_to:
        where += " AND t.fy <= %(hi)s"
        params["hi"] = fy_to
    rows = db.query(
        f"""WITH {_FY_RATE}
            SELECT t.flow, t.fy, t.code, c.description, coalesce(c.display_name, c.description) AS name,
                   c.hs_heading, coalesce(c.unit_clean, nullif(c.unit, '')) AS unit, c.classification, t.country, t.quantity,
                   t.value_pkr_th, r.pkr_per_usd
            FROM trade_hs t JOIN trade_hs_codes c USING (code) LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE {where}
            ORDER BY t.fy DESC, t.code, (t.country <> %(all)s), t.value_pkr_th DESC NULLS LAST
            LIMIT %(limit)s""", params)
    return {"success": True, "data": [
        {"flow": r["flow"], "fiscal_year": _fy_label(r["fy"]), "fy": r["fy"], "code": r["code"],
         "product": r["name"], "product_raw": r["description"], "hs_heading": r["hs_heading"],
         "classification": r["classification"], "country": r["country"],
         "quantity": r["quantity"], "unit": r["unit"] or None, "value_pkr_th": r["value_pkr_th"],
         "value_usd_th": _usd(r["value_pkr_th"], r["pkr_per_usd"])} for r in rows], "count": len(rows)}


@router.get("/partners")
def hs_partners(flow: str = Query("export", pattern=_HS_FLOWS),
                fy: int = Query(..., ge=1991, le=2100, description="Fiscal year (end year: 2025 = FY2024-25)"),
                code: str | None = Query(None, description="Restrict to a product code or prefix*"),
                limit: int = Query(50, ge=1, le=500), caller: Caller = Depends(authenticate)):
    """Partner countries ranked by value for a fiscal year: all products, or one product / prefix."""
    _check_fy(caller, fy)
    params: dict = {"flow": flow, "fy": fy, "limit": limit, "all": _ALL}
    where = "t.flow = %(flow)s AND t.fy = %(fy)s AND t.country <> %(all)s AND t.code <> 'TOTAL'"
    where += _code_filter(code, params)
    rows = cache.cached_json(f"trade:partners:{flow}:{fy}:{params.get('code', '')}:{limit}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE},
            p AS (SELECT t.country, sum(t.value_pkr_th) AS v FROM trade_hs t WHERE {where} GROUP BY 1)
            SELECT p.country, p.v AS value_pkr_th, p.v / NULLIF(sum(p.v) OVER (), 0) * 100 AS share,
                   (SELECT pkr_per_usd FROM fy_rate WHERE fy = %(fy)s) AS rate
            FROM p ORDER BY p.v DESC NULLS LAST LIMIT %(limit)s""", params))
    return {"success": True, "fy": fy, "fiscal_year": _fy_label(fy), "data": [
        {"country": r["country"], "value_pkr_th": r["value_pkr_th"], "value_usd_th": _usd(r["value_pkr_th"], r["rate"]),
         "share_pct": round(float(r["share"]), 3) if r["share"] is not None else None} for r in rows],
        "count": len(rows)}


@router.get("/partner-history")
def hs_partner_history(country: str = Query(...), flow: str = Query("export", pattern=_HS_FLOWS),
                       caller: Caller = Depends(authenticate)):
    """A partner country's total trade with Pakistan each fiscal year (sum over all products)."""
    floor = _fy_floor(caller)
    name = _canon_country(flow, country)
    rows = cache.cached_json(f"trade:partner_history:{flow}:{name}:{floor or 0}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE}
            SELECT t.fy, sum(t.value_pkr_th) AS value_pkr_th, max(r.pkr_per_usd) AS rate
            FROM trade_hs t LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE t.flow = %(flow)s AND t.country = %(country)s AND t.code <> 'TOTAL'
              AND t.fy >= %(lo)s
            GROUP BY t.fy ORDER BY t.fy""",
        {"flow": flow, "country": name, "lo": floor or 0}))
    return {"success": True, "country": country, "flow": flow, "data": [
        {"fy": r["fy"], "fiscal_year": _fy_label(r["fy"]), "value_pkr_th": r["value_pkr_th"],
         "value_usd_th": _usd(r["value_pkr_th"], r["rate"])} for r in rows], "count": len(rows)}


@router.get("/countries")
def hs_countries(flow: str = Query("export", pattern=_HS_FLOWS)):
    """Partner countries in the customs record, with the years they appear. Public."""
    rows = _countries(flow)
    return {"success": True, "data": rows, "count": len(rows)}


@router.get("/partner-products")
def hs_partner_products(country: str = Query(...), flow: str = Query("export", pattern=_HS_FLOWS),
                        fy: int = Query(..., ge=1991, le=2100), limit: int = Query(50, ge=1, le=500),
                        caller: Caller = Depends(authenticate)):
    """A partner country's products ranked by value for a fiscal year."""
    _check_fy(caller, fy)
    name = _canon_country(flow, country)
    rows = cache.cached_json(f"trade:partner_products:{flow}:{fy}:{name}:{limit}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE}
            SELECT t.code, c.description, coalesce(c.display_name, c.description) AS name, c.hs_heading,
                   coalesce(c.unit_clean, nullif(c.unit, '')) AS unit, t.quantity, t.value_pkr_th, r.pkr_per_usd
            FROM trade_hs t JOIN trade_hs_codes c USING (code) LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE t.flow = %(flow)s AND t.fy = %(fy)s AND t.country = %(country)s
              AND t.code <> 'TOTAL'
            ORDER BY t.value_pkr_th DESC NULLS LAST LIMIT %(limit)s""",
        {"flow": flow, "fy": fy, "country": name, "limit": limit}))
    return {"success": True, "fy": fy, "fiscal_year": _fy_label(fy), "country": country, "data": [
        {"code": r["code"], "product": r["name"], "product_raw": r["description"], "hs_heading": r["hs_heading"],
         "unit": r["unit"] or None, "quantity": r["quantity"],
         "value_pkr_th": r["value_pkr_th"], "value_usd_th": _usd(r["value_pkr_th"], r["pkr_per_usd"])}
        for r in rows], "count": len(rows)}
