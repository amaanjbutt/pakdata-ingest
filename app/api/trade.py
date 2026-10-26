"""PBS external trade endpoints.

    GET /v1/trade?flow=export|import&commodity=&group=&from=&to=     monthly, by commodity group
    GET /v1/trade/products?q=                                        product codes (public)
    GET /v1/trade/hs?flow=&code=&country=&from=&to=                  yearly, by product x country
    GET /v1/trade/partners?flow=&fy=&code=                           partners ranked for a year
    GET /v1/trade/partner-products?country=&flow=&fy=                a partner's products ranked
    GET /v1/trade/partner-history?country=&flow=                     a partner's yearly total
    GET /v1/trade/countries?flow=                                    partner list (public)
    GET /v1/trade/product/{code}?fy=                                 one product's page: history, partners, related
    GET /v1/trade/partner/{country}?fy=                              one partner's page: history, rank, products
    GET /v1/trade/pages                                              products + partners with a page (public)
"""
from __future__ import annotations

import re
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app import db
from app.security import Caller, authenticate, history_floor
from app.services import cache, series_repo
from ingestion.trade_names import heading_text, short_title, slugify

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


def _heading_filter(hs4: str, params: dict) -> str:
    """An HS-4 heading's 8-digit codes: a range the primary key can use (LIKE can't under the
    database's en_US collation), kept to 8-digit codes so the older 7-digit SITC codes stay out."""
    params.update(h_lo=hs4, h_hi=hs4 + "9999", h4=hs4)
    return " AND t.code >= %(h_lo)s AND t.code <= %(h_hi)s AND length(t.code) = 8 AND left(t.code, 4) = %(h4)s"


def _usd(v, rate):
    return round(float(v) / float(rate), 3) if v is not None and rate else None


def _all_codes() -> list[dict]:
    return cache.cached_json("trade:hs_codes:v2", 3600, lambda: db.query(
        "SELECT code, classification, coalesce(display_name, description) AS name, short_name, "
        "hs_heading, description, coalesce(unit_clean, nullif(unit, '')) AS unit, first_fy, last_fy "
        "FROM trade_hs_codes WHERE classification <> 'total' ORDER BY code"))


@router.get("/products")
def hs_products(q: str | None = Query(None, description="Words to find in the product name or heading, or a code prefix"),
                classification: str | None = Query(None, pattern="^(HS|SITC)$"),
                limit: int = Query(50, ge=1, le=1000)):
    """Product codes in the customs record: 8-digit HS-based codes (FY2002-03 on) and 7-digit
    SITC-based codes (FY1990-91 to FY2001-02), with a readable `name` (the official HS/SITC text
    where the code is the official item, otherwise PBS's wording spelled out), the official
    parent `hs_heading`, and PBS's raw abbreviated text in `description`. Public."""
    rows = _all_codes()
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
    rows = _rank_partners(flow, fy, code, limit)
    return {"success": True, "fy": fy, "fiscal_year": _fy_label(fy), "data": rows, "count": len(rows)}


def _rank_partners(flow: str, fy: int, code: str | None, limit: int, heading: str | None = None) -> list[dict]:
    """Partners ranked by value for a year: all products, a code / prefix*, or an HS-4 heading
    (its 8-digit codes only — a bare prefix would also catch the older 7-digit SITC codes)."""
    params: dict = {"flow": flow, "fy": fy, "limit": limit, "all": _ALL}
    where = "t.flow = %(flow)s AND t.fy = %(fy)s AND t.country <> %(all)s AND t.code <> 'TOTAL'"
    if heading:
        where += _heading_filter(heading, params)
    else:
        where += _code_filter(code, params)
    key = f"h{heading}" if heading else params.get("code", "")
    rows = cache.cached_json(f"trade:partners:{flow}:{fy}:{key}:{limit}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE},
            p AS (SELECT t.country, sum(t.value_pkr_th) AS v FROM trade_hs t WHERE {where} GROUP BY 1)
            SELECT p.country, p.v AS value_pkr_th, p.v / NULLIF(sum(p.v) OVER (), 0) * 100 AS share,
                   (SELECT pkr_per_usd FROM fy_rate WHERE fy = %(fy)s) AS rate
            FROM p ORDER BY p.v DESC NULLS LAST LIMIT %(limit)s""", params))
    return [{"country": r["country"], "value_pkr_th": r["value_pkr_th"], "value_usd_th": _usd(r["value_pkr_th"], r["rate"]),
             "share_pct": round(float(r["share"]), 3) if r["share"] is not None else None} for r in rows]


@router.get("/partner-history")
def hs_partner_history(country: str = Query(...), flow: str = Query("export", pattern=_HS_FLOWS),
                       caller: Caller = Depends(authenticate)):
    """A partner country's total trade with Pakistan each fiscal year (sum over all products)."""
    rows = _partner_history(flow, _canon_country(flow, country), _fy_floor(caller) or 0)
    return {"success": True, "country": country, "flow": flow, "data": rows, "count": len(rows)}


def _partner_history(flow: str, name: str, floor: int = 0) -> list[dict]:
    rows = cache.cached_json(f"trade:partner_history:{flow}:{name}:{floor}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE}
            SELECT t.fy, sum(t.value_pkr_th) AS value_pkr_th, max(r.pkr_per_usd) AS rate
            FROM trade_hs t LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE t.flow = %(flow)s AND t.country = %(country)s AND t.code <> 'TOTAL'
              AND t.fy >= %(lo)s
            GROUP BY t.fy ORDER BY t.fy""",
        {"flow": flow, "country": name, "lo": floor}))
    return [{"fy": r["fy"], "fiscal_year": _fy_label(r["fy"]), "value_pkr_th": r["value_pkr_th"],
             "value_usd_th": _usd(r["value_pkr_th"], r["rate"])} for r in rows]


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
    rows = _partner_products(flow, fy, _canon_country(flow, country), limit)
    return {"success": True, "fy": fy, "fiscal_year": _fy_label(fy), "country": country, "data": rows,
            "count": len(rows)}


def _partner_products(flow: str, fy: int, name: str, limit: int) -> list[dict]:
    rows = cache.cached_json(f"trade:partner_products:{flow}:{fy}:{name}:{limit}", _TTL, lambda: db.query(
        f"""WITH {_FY_RATE}
            SELECT t.code, c.description, coalesce(c.display_name, c.description) AS name, c.hs_heading,
                   coalesce(c.unit_clean, nullif(c.unit, '')) AS unit, t.quantity, t.value_pkr_th, r.pkr_per_usd
            FROM trade_hs t JOIN trade_hs_codes c USING (code) LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE t.flow = %(flow)s AND t.fy = %(fy)s AND t.country = %(country)s
              AND t.code <> 'TOTAL'
            ORDER BY t.value_pkr_th DESC NULLS LAST LIMIT %(limit)s""",
        {"flow": flow, "fy": fy, "country": name, "limit": limit}))
    return [{"code": r["code"], "product": r["name"], "product_raw": r["description"], "hs_heading": r["hs_heading"],
             "unit": r["unit"] or None, "quantity": r["quantity"],
             "value_pkr_th": r["value_pkr_th"], "value_usd_th": _usd(r["value_pkr_th"], r["pkr_per_usd"])}
            for r in rows]


# ---- Pages on pakdatahub.com: /trade/products/{code}-{slug} and /trade/partners/{slug} ---------
# Every HS-4 heading (8-digit codes, FY2002-03 on), every product code and every partner has a
# page. The ones with enough trade to say something are indexable — in the sitemap and the search
# index, without `noindex` — the rest render for whoever follows a link.

RECENT = 3                    # "recent trade" = a year's average over the last three fiscal years
HEADING_MIN_USD_TH = 1_000    # a heading page is indexable from US$1 million a year;
CODE_MIN_USD_TH = 10_000      # a product code's from US$10 million a year, when its name reads cleanly
CODE_MAX_SHARE = 0.9          # and it is under 90% of its heading (else the two pages show the same figures);
PARTNER_MIN_USD_TH = 10_000   # a partner's from US$10 million of trade over all the years
# PBS fragments that make poor page titles ("S-cotton yarn UCF.d-232.56-714.29", "Jersey, etc.. other")
_JUNK_NAME = re.compile(r"\d|[A-Za-z]\.[A-Za-z]|\.\.")
_NO_PAGE_PARTNERS = re.compile(r"\(not specified\)|^Pakistan$|^Other \(origin")
_FLOWS2 = ("export", "import")


def heading_name(hs4: str) -> str:
    t = heading_text(hs4)
    return short_title(t) if t else f"HS heading {hs4}"


def chapter(hs2: str) -> dict:
    t = heading_text(hs2)
    return {"code": hs2, "name": short_title(t) if t else f"Chapter {hs2}"}


def product_path(code: str, name: str) -> str:
    slug = slugify(name)
    return f"/trade/products/{code}-{slug}" if slug else f"/trade/products/{code}"


def partner_path(country: str) -> str:
    return f"/trade/partners/{slugify(country)}"


def _latest_fy() -> int:
    return cache.cached_json("trade:latest_fy:v1", _TTL, lambda: (db.query_one(
        "SELECT max(fy) AS fy FROM trade_hs WHERE flow IN ('export', 'import')") or {}).get("fy") or 0)


def _recent(vals: list | None) -> tuple[float, float]:
    return (vals[0], vals[1]) if vals else (0.0, 0.0)


def page_index() -> dict:
    """Recent trade (US$ thousand a year: exports, imports) of every 8-digit code and HS-4 heading
    that traded in the last RECENT fiscal years: {"latest_fy", "codes": {code: [ex, im]}, "headings": {…}}."""
    def build() -> dict:
        latest = _latest_fy()
        rows = db.query(
            f"""WITH {_FY_RATE}
                SELECT t.code, t.flow, sum(t.value_pkr_th / r.pkr_per_usd) / {RECENT} AS usd_th
                FROM trade_hs t JOIN fy_rate r ON r.fy = t.fy
                WHERE t.country = %(all)s AND t.code <> 'TOTAL' AND t.flow IN ('export', 'import')
                  AND t.fy > %(lo)s AND length(t.code) = 8
                GROUP BY 1, 2""", {"all": _ALL, "lo": latest - RECENT})
        codes: dict[str, list[float]] = {}
        heads: dict[str, list[float]] = {}
        for r in rows:
            i = 0 if r["flow"] == "export" else 1
            v = float(r["usd_th"] or 0)
            codes.setdefault(r["code"], [0.0, 0.0])[i] += v
            heads.setdefault(r["code"][:4], [0.0, 0.0])[i] += v
        def rnd(d):
            return {k: [round(a, 1), round(b, 1)] for k, (a, b) in d.items()}
        return {"latest_fy": latest, "codes": rnd(codes), "headings": rnd(heads)}
    return cache.cached_json("trade:page_index:v1", _TTL, build)


def partner_index() -> dict[str, dict]:
    """Every partner: years present, recent trade a year and total over all years (US$ thousand),
    page path and whether it is indexable. One pass over the table (~1 s), cached."""
    def build() -> dict:
        latest = _latest_fy()
        rows = db.query(
            f"""WITH {_FY_RATE},
                y AS (SELECT t.country, t.fy, t.flow, sum(t.value_pkr_th) AS v FROM trade_hs t
                      WHERE t.flow IN ('export', 'import') AND t.country <> %(all)s AND t.code <> 'TOTAL'
                      GROUP BY 1, 2, 3)
                SELECT y.country, min(y.fy) AS first_fy, max(y.fy) AS last_fy,
                       sum(y.v / r.pkr_per_usd) FILTER (WHERE y.flow = 'export' AND y.fy > %(lo)s) / {RECENT} AS ex,
                       sum(y.v / r.pkr_per_usd) FILTER (WHERE y.flow = 'import' AND y.fy > %(lo)s) / {RECENT} AS im,
                       sum(y.v / r.pkr_per_usd) AS total
                FROM y LEFT JOIN fy_rate r ON r.fy = y.fy GROUP BY 1 ORDER BY 1""",
            {"all": _ALL, "lo": latest - RECENT})
        return {r["country"]: {
            "country": r["country"], "first_fy": r["first_fy"], "last_fy": r["last_fy"],
            "export_usd_th": round(float(r["ex"] or 0), 1), "import_usd_th": round(float(r["im"] or 0), 1),
            "total_usd_th": round(float(r["total"] or 0), 1), "path": partner_path(r["country"]),
            "indexable": float(r["total"] or 0) >= PARTNER_MIN_USD_TH and not _NO_PAGE_PARTNERS.search(r["country"]),
        } for r in rows}
    return cache.cached_json("trade:partner_index:v1", _TTL, build)


def code_indexable(row: dict, idx: dict) -> bool:
    """An 8-digit code's page is indexable: enough trade, a clean name, and not its whole heading."""
    if row.get("classification") != "HS":
        return False
    ex, im = _recent(idx["codes"].get(row["code"]))
    hx, hm = _recent(idx["headings"].get(row["code"][:4]))
    official = row.get("name") == row.get("hs_heading")
    return (ex + im >= CODE_MIN_USD_TH and (ex + im) < CODE_MAX_SHARE * (hx + hm)
            and (official or not _JUNK_NAME.search(row.get("name") or "")))


def heading_indexable(hs4: str, idx: dict) -> bool:
    return sum(_recent(idx["headings"].get(hs4))) >= HEADING_MIN_USD_TH


def _product_ref(row: dict, idx: dict) -> dict:
    ex, im = _recent(idx["codes"].get(row["code"]))
    return {"code": row["code"], "name": row["name"], "path": product_path(row["code"], row["name"]),
            "indexable": code_indexable(row, idx), "export_usd_th": ex, "import_usd_th": im}


def _heading_ref(hs4: str, idx: dict) -> dict:
    name = heading_name(hs4)
    ex, im = _recent(idx["headings"].get(hs4))
    return {"code": hs4, "name": name, "path": product_path(hs4, name), "indexable": heading_indexable(hs4, idx),
            "export_usd_th": ex, "import_usd_th": im}


def _history(where: str, params: dict, with_quantity: bool) -> list[dict]:
    """Yearly exports and imports (the 'All countries' lines) for a code or heading."""
    rows = db.query(
        f"""WITH {_FY_RATE}
            SELECT t.fy, t.flow, sum(t.value_pkr_th) AS pkr, sum(t.quantity) AS qty, max(r.pkr_per_usd) AS rate
            FROM trade_hs t LEFT JOIN fy_rate r ON r.fy = t.fy
            WHERE t.country = %(all)s AND t.flow IN ('export', 'import') {where}
            GROUP BY 1, 2 ORDER BY 1""", {**params, "all": _ALL})
    years: dict[int, dict] = {}
    for r in rows:
        y = years.setdefault(r["fy"], {"fy": r["fy"], "fiscal_year": _fy_label(r["fy"]),
                                       **{f"{f}_{k}": None for f in _FLOWS2 for k in ("pkr_th", "usd_th", "quantity")}})
        y[f"{r['flow']}_pkr_th"] = r["pkr"]
        y[f"{r['flow']}_usd_th"] = _usd(r["pkr"], r["rate"])
        y[f"{r['flow']}_quantity"] = r["qty"] if with_quantity else None
    return [years[k] for k in sorted(years)]


def _by_recent(rows: list[dict], idx: dict) -> list[dict]:
    traded = [r for r in rows if sum(_recent(idx["codes"].get(r["code"])))]
    return sorted(traded, key=lambda r: -sum(_recent(idx["codes"].get(r["code"]))))


def _product_page(code: str, fy: int | None) -> dict | None:
    idx = page_index()
    codes = _all_codes()
    if len(code) == 4:
        members = [r for r in codes if r["classification"] == "HS" and len(r["code"]) == 8 and r["code"][:4] == code]
        if not members:
            return None
        params: dict = {}
        history = _history(_heading_filter(code, params), params, with_quantity=False)
        ref = _heading_ref(code, idx)
        page = {"code": code, "level": "heading", "classification": "HS", "name": ref["name"],
                "official_name": heading_text(code), "hs6_heading": None, "pbs_name": None, "unit": None,
                "chapter": chapter(code[:2]), "heading": None,
                "first_fy": min(r["first_fy"] or 9999 for r in members),
                "last_fy": max(r["last_fy"] or 0 for r in members)}
        related = [_product_ref(r, idx) for r in _by_recent(members, idx)[:30]]
        rank_by: dict = {"heading": code}
    else:
        row = next((r for r in codes if r["code"] == code), None)
        if row is None:
            return None
        history = _history("AND t.code = %(code)s", {"code": code}, with_quantity=True)
        ref = _product_ref(row, idx)
        hs = row["classification"] == "HS"
        page = {"code": code, "level": "code", "classification": row["classification"], "name": row["name"],
                "official_name": row["name"] if row["name"] == row["hs_heading"] else None,
                "hs6_heading": row["hs_heading"], "pbs_name": row["description"], "unit": row["unit"],
                "chapter": chapter(code[:2]) if hs else None,
                "heading": _heading_ref(code[:4], idx) if hs else None,
                "first_fy": row["first_fy"], "last_fy": row["last_fy"]}
        siblings = [r for r in codes if hs and r["code"] != code and len(r["code"]) == 8 and r["code"][:4] == code[:4]]
        related = [_product_ref(r, idx) for r in _by_recent(siblings, idx)[:12]]
        rank_by = {"code": code}
    traded = [h["fy"] for h in history if h["export_pkr_th"] or h["import_pkr_th"]]
    year = fy or (max(traded) if traded else idx["latest_fy"])
    partners = {f: [{**p, "path": partner_path(p["country"])}
                    for p in _rank_partners(f, year, rank_by.get("code"), 25, rank_by.get("heading"))]
                for f in _FLOWS2}
    return {**page, "slug": ref["path"].rsplit("/", 1)[-1], "path": ref["path"], "indexable": ref["indexable"],
            "fy": year, "fiscal_year": _fy_label(year), "latest_fy": idx["latest_fy"],
            "recent": {"years": RECENT, "export_usd_th": ref["export_usd_th"], "import_usd_th": ref["import_usd_th"]},
            "history": history, "partners": partners, "related": related}


@router.get("/product/{code}")
def product_page(code: str,
                 fy: int | None = Query(None, ge=1991, le=2100,
                                        description="Year for the partner ranking (default: the latest the product traded)"),
                 caller: Caller = Depends(authenticate)):
    """One product in one call: an HS-4 heading (`1006`: its 8-digit codes summed, FY2002-03 on) or
    a product code (`10063010`). Names, yearly exports and imports, the partners for a year, and
    related products — what pakdatahub.com/trade/products/{code} shows."""
    c = code.strip().split("-", 1)[0]
    if not re.fullmatch(r"\d{4}|\d{7,8}", c):
        raise HTTPException(status_code=404, detail="give a 4-digit HS heading or a product code")
    if fy is not None:
        _check_fy(caller, fy)
    data = cache.cached_json(f"trade:product_page:v1:{c}:{fy or 0}", _TTL, lambda: _product_page(c, fy))
    if data is None:
        raise HTTPException(status_code=404, detail=f"no product {c} in the customs record")
    floor = _fy_floor(caller)
    if floor:
        data["history"] = [h for h in data["history"] if h["fy"] >= floor]
    return {"success": True, "data": data}


def _partner_page(name: str, fy: int | None) -> dict:
    p = partner_index()[name]
    latest = _latest_fy()
    years: dict[int, dict] = {}
    for f in _FLOWS2:
        for h in _partner_history(f, name):
            y = years.setdefault(h["fy"], {"fy": h["fy"], "fiscal_year": h["fiscal_year"],
                                           **{f"{g}_{k}": None for g in _FLOWS2 for k in ("pkr_th", "usd_th")}})
            y[f"{f}_pkr_th"], y[f"{f}_usd_th"] = h["value_pkr_th"], h["value_usd_th"]
    year = fy or min(p["last_fy"], latest)
    rank, share, ranked, products = {}, {}, {}, {}
    for f in _FLOWS2:
        ranking = _rank_partners(f, year, None, 500)
        pos = next((i for i, r in enumerate(ranking) if r["country"] == name), None)
        rank[f] = pos + 1 if pos is not None else None
        share[f] = ranking[pos]["share_pct"] if pos is not None else None
        ranked[f] = len(ranking)
        products[f] = [{**r, "path": product_path(r["code"], r["product"] or r["code"]),
                        "heading": r["code"][:4] if len(r["code"]) == 8 else None}
                       for r in _partner_products(f, year, name, 25)]
    return {**p, "slug": p["path"].rsplit("/", 1)[-1], "fy": year, "fiscal_year": _fy_label(year), "latest_fy": latest,
            "rank": rank, "share_pct": share, "partners_ranked": ranked,
            "history": [years[k] for k in sorted(years)], "products": products}


@router.get("/partner/{country}")
def partner_page(country: str,
                 fy: int | None = Query(None, ge=1991, le=2100, description="Year for the product ranking (default: the latest)"),
                 caller: Caller = Depends(authenticate)):
    """One partner in one call (`china`, `united-states`, or the name): yearly exports and imports,
    its rank among Pakistan's partners and its top products for a year — what
    pakdatahub.com/trade/partners/{slug} shows."""
    want = slugify(country)
    name = next((c for c in partner_index() if slugify(c) == want), None)
    if name is None:
        raise HTTPException(status_code=404, detail=f"no trade partner '{country}'")
    if fy is not None:
        _check_fy(caller, fy)
    data = cache.cached_json(f"trade:partner_page:v1:{want}:{fy or 0}", _TTL, lambda: _partner_page(name, fy))
    floor = _fy_floor(caller)
    if floor:
        data["history"] = [h for h in data["history"] if h["fy"] >= floor]
    return {"success": True, "data": data}


def _pages() -> dict:
    idx = page_index()
    headings = [{**_heading_ref(h, idx), "chapter": h[:2]} for h in idx["headings"]]
    products = [{**_product_ref(r, idx), "heading": r["code"][:4]} for r in _all_codes()
                if r["code"] in idx["codes"] and code_indexable(r, idx)]
    partners = [p for p in partner_index().values() if p["indexable"]]
    for rows in (headings, products, partners):
        rows.sort(key=lambda r: -(r["export_usd_th"] + r["import_usd_th"]))
    return {"latest_fy": idx["latest_fy"], "recent_years": RECENT,
            "chapters": {c: chapter(c)["name"] for c in sorted({h[:2] for h in idx["headings"]})},
            "data": {
                "headings": [{k: v for k, v in h.items() if k != "indexable"} for h in headings if h["indexable"]],
                "products": [{k: v for k, v in r.items() if k != "indexable"} for r in products],
                "partners": [{k: p[k] for k in ("country", "path", "first_fy", "last_fy", "export_usd_th", "import_usd_th")}
                             for p in partners]}}


@router.get("/pages")
def trade_pages():
    """The product headings, product codes and partners with their own indexable page on
    pakdatahub.com, with recent trade (a year's average over the last three fiscal years, US$
    thousand). Public."""
    data = cache.cached_json("trade:pages:v1", _TTL, _pages)
    return {"success": True, **data, "count": sum(len(v) for v in data["data"].values())}
