"""Trade product and partner pages: names, slugs, and the one-call page endpoints."""
from __future__ import annotations

import pytest

from ingestion.trade_names import heading_text, short_title, slugify
from tests.test_api_integration import _auth, _db_available, api_key, client  # noqa: F401 — fixtures


def test_heading_titles_from_the_official_text():
    assert heading_text("1006") == "Rice" and heading_text("10") == "Cereals"
    assert short_title(heading_text("5201")) == "Cotton, not carded or combed"            # qualifier kept
    assert short_title(heading_text("6109")) == "T-shirts, singlets and other vests, knitted or crocheted"
    # long lists are cut at a comma, parentheticals dropped
    assert short_title(heading_text("6203")) == "Suits, ensembles, jackets, blazers, trousers, men's or boys'"
    assert short_title(heading_text("27")) == "Mineral fuels, mineral oils and products of their distillation"
    assert all(len(short_title(heading_text(h))) <= 70 for h in ("2710", "8517", "3004", "0306", "8541"))


def test_slugs_match_the_frontend_rule():
    # frontend/src/lib/pakdata/trade.ts `slugify` must give the same answers
    assert slugify("Rice basmati") == "rice-basmati"
    assert slugify("Men's or boys' suits & ensembles") == "mens-or-boys-suits-and-ensembles"
    assert slugify("Cote d'Ivoire") == "cote-divoire" and slugify("Other Asia (Taiwan)") == "other-asia-taiwan"
    assert slugify("Cotton yarn, containing 85% or more by weight of cotton") == \
        "cotton-yarn-containing-85-or-more-by-weight-of-cotton"
    long = slugify("Petroleum oils and oils obtained from bituminous minerals, crude")
    assert long == "petroleum-oils-and-oils-obtained-from-bituminous-minerals" and len(long) <= 60
    assert slugify("???") == ""


CODES = [("99011010", "HS", "TESTWIDGETS", "Testwidgets"), ("99011020", "HS", "OTH TESTWIDGETS", "Other testwidgets"),
         ("99021000", "HS", "TESTGADGETS UCF.D-1.5", "Testgadgets UCF.d-1.5")]
FY = 2099
RATE = 250.0
# Rs thousand; at Rs 250/US$ and a three-year average, 7.5m = US$10m a year (the code threshold)
ROWS = [("export", FY, "99011010", "All countries", 10, 30_000_000.0), ("export", FY, "99011010", "Zz-Testland", 6, 20_000_000.0),
        ("export", FY, "99011010", "Zz-Otherland", 4, 10_000_000.0),
        ("export", FY, "99011020", "All countries", 5, 9_000_000.0), ("export", FY, "99011020", "Zz-Otherland", 5, 9_000_000.0),
        ("import", FY, "99021000", "All countries", 1, 8_000_000.0), ("import", FY, "99021000", "Zz-Testland", 1, 8_000_000.0),
        ("export", FY, "TOTAL", "All countries", None, 39_000_000.0), ("import", FY, "TOTAL", "All countries", None, 8_000_000.0)]


@pytest.fixture
def trade_fixture():
    from app import db
    from app.services import cache

    def forget():
        try:
            r = cache.get_redis()
            for k in r.scan_iter("cache:v1:trade:*"):
                r.delete(k)
        except Exception:
            pass

    had_series = db.query_one("SELECT 1 AS x FROM series WHERE id = 'fx.rate.avg.usd'")
    if not had_series:
        db.execute("INSERT INTO series (id, module, name, unit, frequency, source, tier, is_active) VALUES "
                   "('fx.rate.avg.usd','forex','PKR per USD, average','pkr','monthly','SBP','basic',true)")
    db.execute("INSERT INTO observations (series_id, obs_date, value, dims) VALUES ('fx.rate.avg.usd', %s, %s, '{}') "
               "ON CONFLICT DO NOTHING", (f"{FY - 1}-12-31", RATE))
    for code, cl, raw, name in CODES:
        db.execute("INSERT INTO trade_hs_codes (code, classification, description, display_name, first_fy, last_fy) "
                   "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING", (code, cl, raw, name, FY, FY))
    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany("INSERT INTO trade_hs VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ROWS)
    forget()
    try:
        yield
    finally:
        db.execute("DELETE FROM trade_hs WHERE fy = %s", (FY,))
        db.execute("DELETE FROM trade_hs_codes WHERE code = ANY(%s)", ([c[0] for c in CODES],))
        db.execute("DELETE FROM observations WHERE series_id = 'fx.rate.avg.usd' AND obs_date = %s", (f"{FY - 1}-12-31",))
        if not had_series:
            db.execute("DELETE FROM series WHERE id = 'fx.rate.avg.usd'")
        forget()


@pytest.mark.skipif(not _db_available(), reason="no database")
def test_product_heading_and_code_pages(client, api_key, trade_fixture):  # noqa: F811
    r = client.get("/v1/trade/product/9901", headers=_auth(api_key))
    assert r.status_code == 200, r.text
    h = r.json()["data"]
    assert h["level"] == "heading" and h["fy"] == FY and h["path"] == "/trade/products/9901-hs-heading-9901"
    year = h["history"][-1]
    assert year["fy"] == FY and year["export_pkr_th"] == 39_000_000.0 and year["export_usd_th"] == 156_000.0
    assert year["export_quantity"] is None                                     # units differ across codes
    assert [(p["country"], p["path"]) for p in h["partners"]["export"]] == [
        ("Zz-Testland", "/trade/partners/zz-testland"), ("Zz-Otherland", "/trade/partners/zz-otherland")]
    assert [x["code"] for x in h["related"]] == ["99011010", "99011020"]
    assert h["indexable"] and h["chapter"]["code"] == "99"                    # US$52m a year

    r = client.get("/v1/trade/product/99011010-old-slug", headers=_auth(api_key))
    c = r.json()["data"]
    assert c["level"] == "code" and c["path"] == "/trade/products/99011010-testwidgets" and c["slug"] == "99011010-testwidgets"
    assert c["heading"]["code"] == "9901" and c["history"][-1]["export_quantity"] == 10
    assert [p["share_pct"] for p in c["partners"]["export"]] == [66.667, 33.333]
    assert c["indexable"]                                                      # US$40m a year, 77% of its heading
    assert client.get("/v1/trade/product/99011020", headers=_auth(api_key)).json()["data"]["indexable"]  # US$12m, 23%
    junk = client.get("/v1/trade/product/99021000", headers=_auth(api_key)).json()["data"]
    assert junk["indexable"] is False                                          # all of its heading, and a PBS fragment name

    assert client.get("/v1/trade/product/12", headers=_auth(api_key)).status_code == 404
    assert client.get("/v1/trade/product/99999999", headers=_auth(api_key)).status_code == 404


@pytest.mark.skipif(not _db_available(), reason="no database")
def test_partner_page_and_pages_list(client, api_key, trade_fixture):  # noqa: F811
    r = client.get("/v1/trade/partner/zz-testland", headers=_auth(api_key))
    assert r.status_code == 200, r.text
    p = r.json()["data"]
    assert p["country"] == "Zz-Testland" and p["path"] == "/trade/partners/zz-testland" and p["fy"] == FY
    assert p["rank"] == {"export": 1, "import": 1} and p["share_pct"]["import"] == 100.0
    assert p["history"][-1]["export_pkr_th"] == 20_000_000.0 and p["history"][-1]["import_usd_th"] == 32_000.0
    assert [(x["code"], x["path"], x["heading"]) for x in p["products"]["export"]] == [
        ("99011010", "/trade/products/99011010-testwidgets", "9901")]
    assert client.get("/v1/trade/partner/Zz-Testland", headers=_auth(api_key)).json()["data"]["slug"] == "zz-testland"
    assert client.get("/v1/trade/partner/nowhere-land", headers=_auth(api_key)).status_code == 404

    body = client.get("/v1/trade/pages").json()                                # public
    assert body["latest_fy"] == FY
    heads = {h["code"]: h for h in body["data"]["headings"]}
    assert "9901" in heads and heads["9901"]["chapter"] == "99"
    assert body["chapters"]["99"] == "Commodities not specified according to kind"
    codes = {c["code"] for c in body["data"]["products"]}
    assert {"99011010", "99011020"} <= codes and "99021000" not in codes
    partners = {x["country"]: x for x in body["data"]["partners"]}
    assert partners["Zz-Testland"]["path"] == "/trade/partners/zz-testland"
