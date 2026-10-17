"""The search index for everything that is not a series: one row per findable thing.

`/v1/search?kinds=all` (and the site's ⌘K, /coverage and dashboard search) looks these up
next to the series catalog: trade products and partner countries, districts, power plants,
mandi crops and markets, mutual funds, AMCs and debt securities. Each row carries the page
it opens (`url`) and a `haystack` of every name it goes by (official text, PBS's abbreviation,
Urdu names, plant aliases), trigram-indexed.

`refresh()` rebuilds the table in one transaction (~25k rows, a few seconds); the scheduler
runs it nightly and the loaders call it after a load.
"""
from __future__ import annotations

import logging
import math
import re
from urllib.parse import quote

from app import db

log = logging.getLogger(__name__)

KINDS = ("trade_product", "trade_country", "district", "plant", "mandi_crop", "mandi_market",
         "fund", "amc", "security")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _hay(*parts) -> str:
    return " ".join(str(p) for p in parts if p).lower()


def _rows_trade(cur) -> list[tuple]:
    cur.execute("SELECT max(fy) FROM trade_hs")
    fy = (cur.fetchone() or [None])[0]
    cur.execute("""SELECT code, sum(value_pkr_th) FROM trade_hs
                   WHERE fy >= %s AND country = 'All countries' AND code <> 'TOTAL' GROUP BY code""",
                ((fy or 0) - 2,))
    value = {c: float(v or 0) for c, v in cur.fetchall()}
    cur.execute("""SELECT code, classification, coalesce(display_name, description), short_name, description,
                          hs_heading, first_fy, last_fy
                   FROM trade_hs_codes WHERE classification <> 'total'""")
    out = []
    for code, cl, name, short, raw, heading, f0, f1 in cur.fetchall():
        label = "HS" if cl == "HS" else "SITC"
        years = f"FY{(f0 or 0) - 1}-{str(f1 or 0)[2:]}" if f0 and f1 else ""
        out.append(("trade_product", code, name, f"Traded product · {label} {code} · {years}".strip(" ·"),
                    f"/trade?code={code}", math.log10(1 + value.get(code, 0)),
                    _hay(name, short, raw, heading, code, "export import trade")))
    cur.execute("SELECT country, min(fy), max(fy) FROM trade_hs WHERE country <> 'All countries' GROUP BY 1")
    for country, f0, f1 in cur.fetchall():
        out.append(("trade_country", country, f"Trade with {country}",
                    f"Exports and imports by product, FY{f0 - 1}–{str(f1)[2:]}",
                    f"/trade?country={quote(country)}", 3.0, _hay(country, "trade exports imports partner")))
    return out


def _rows_districts(cur) -> list[tuple]:
    from app.api.regional import _districts

    out = []
    for d in _districts():
        pop = d.get("population")
        sub = f"District, {d['province']}" + (f" · population {int(pop):,}" if pop else "")
        out.append(("district", f"{d['province']}|{d['district']}", d["district"], sub,
                    f"/districts/{_slug(d['district'])}", 4.0 + math.log10(1 + (pop or 0)) / 2,
                    _hay(d["district"], d["province"], "district population census literacy profile")))
    return out


def _rows_plants(cur) -> list[tuple]:
    from ingestion.plants_pk import PLANTS

    cur.execute("""SELECT DISTINCT dims->>'plant' FROM observations
                   WHERE series_id IN ('energy.generation.plant', 'energy.generation.plant_daily',
                                       'energy.power.merit_order.specific_cost')""")
    present = {r[0] for r in cur.fetchall()}
    out = []
    for p in PLANTS:
        if p.name not in present:
            continue
        out.append(("plant", p.name, p.name, f"Power plant · {p.fuel}",
                    "/series/energy.generation.plant", 3.0,
                    _hay(p.name, p.fuel, *p.aliases, "power plant electricity generation")))
    return out


def _rows_mandi(cur) -> list[tuple]:
    cur.execute("""SELECT coalesce(crop_key, slug), province, coalesce(display_name, name), name, urdu_name, category
                   FROM agri_commodities""")
    crops: dict[str, dict] = {}
    for crop, prov, name, raw, urdu, cat in sorted(cur.fetchall(), key=lambda r: r[1] != "Punjab"):
        c = crops.setdefault(crop, {"name": name, "cat": cat, "provinces": set(), "words": []})
        c["provinces"].add(prov)
        c["words"] += [name, raw, urdu]
    out = []
    for crop, c in crops.items():
        where = " and ".join(sorted(c["provinces"]))
        out.append(("mandi_crop", crop, f"{c['name']} — mandi rates", f"Wholesale prices · {where}",
                    f"/mandi/{crop}", 4.0, _hay(*c["words"], c["cat"], "mandi wholesale price rate")))
    cur.execute("SELECT slug, coalesce(display_name, name), name, province, coalesce(region, province), is_major "
                "FROM agri_markets")
    for slug, name, raw, prov, region, major in cur.fetchall():
        url = f"/mandi?market={slug}" + ("&province=sindh" if prov == "Sindh" else "")
        # Punjab's daily report also covers Karachi, Hyderabad, Quetta and Gwadar
        title = f"{name} mandi" if region == prov else f"{name} mandi (Punjab's report)"
        out.append(("mandi_market", slug, title, f"Wholesale market · {region}", url,
                    3.5 if major else 2.5 if region == prov else 1.5, _hay(name, raw, region, "mandi market wholesale")))
    return out


def _rows_funds(cur) -> list[tuple]:
    cur.execute("""SELECT f.fund_id, f.name, f.amc, f.category FROM funds f
                   WHERE EXISTS (SELECT 1 FROM fund_navs n WHERE n.fund_id = f.fund_id
                                 AND n.obs_date > current_date - 60)""")
    out, amcs = [], {}
    for fid, name, amc, cat in cur.fetchall():
        out.append(("fund", str(fid), name, f"Mutual fund · {amc or ''}{' · ' + cat if cat else ''}",
                    f"/funds/{fid}", 3.0, _hay(name, amc, cat, "mutual fund nav")))
        if amc:
            amcs[amc] = amcs.get(amc, 0) + 1
    for amc, n in amcs.items():
        out.append(("amc", amc, amc, f"Asset manager · {n} funds", f"/amcs/{quote(amc, safe='')}", 3.5,
                    _hay(amc, "asset management company amc")))
    cur.execute("SELECT code, security_type, coalesce(name, sbp_code, code) FROM securities")
    for code, typ, name in cur.fetchall():
        label = {"pib_floating": "Floating-rate PIB", "gis_sukuk": "GoP Ijara Sukuk",
                 "tfc_sukuk": "Corporate TFC / sukuk"}.get(typ, "Debt security")
        out.append(("security", code, name if name != code else code, f"{label} · {code}",
                    f"/securities/{quote(code, safe='/')}", 2.0, _hay(code, name, label, "bond debt security")))
    return out


BUILDERS = (_rows_trade, _rows_districts, _rows_plants, _rows_mandi, _rows_funds)


def refresh() -> dict:
    """Rebuild search_entities in one transaction. A builder that fails keeps its old rows."""
    counts: dict[str, int] = {}
    with db.connection() as conn:
        with conn.cursor() as cur:
            for build in BUILDERS:
                try:
                    with conn.transaction():
                        rows = build(cur)
                        kinds = sorted({r[0] for r in rows})
                        if not rows:
                            continue
                        cur.execute("DELETE FROM search_entities WHERE kind = ANY(%s)", (kinds,))
                        with cur.copy("COPY search_entities (kind, key, title, subtitle, url, boost, haystack) "
                                      "FROM STDIN") as cp:
                            for r in rows:
                                cp.write_row(r)
                        for k in kinds:
                            counts[k] = sum(1 for r in rows if r[0] == k)
                except Exception:  # noqa: BLE001 — one source must not empty the whole index
                    log.exception("search_entities: %s failed", build.__name__)
        conn.commit()
    log.info("search_entities.refresh: %s", counts)
    return counts
