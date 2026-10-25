from ingestion.trade_names import clean, expand, official_heading, split_unit


def test_split_unit_glued_into_description():
    # old fixed-width SITC rows: 30 description characters + the unit, unit column empty
    assert split_unit("INST DESIGNED FOR DEMONSTRATONNO", "", "SITC") == ("INST DESIGNED FOR DEMONSTRATON", "NO")
    assert split_unit("F-ROLL HIGH SPEED STEEL <600MMMT", "", "SITC") == ("F-ROLL HIGH SPEED STEEL <600MM", "MT")


def test_split_unit_description_tail_in_unit_column():
    assert split_unit("MONO,DI-/ TRICHLOROACETIC", "ACIDKG") == ("MONO,DI-/ TRICHLOROACETIC ACID", "KG")
    assert split_unit("OTHER FISH FRESH OR CHILLED", "NSKG") == ("OTHER FISH FRESH OR CHILLED NS", "KG")
    assert split_unit("HELICOPTERS-WEIGHT UPTO2000", "KGNO") == ("HELICOPTERS-WEIGHT UPTO2000 KG", "NO")
    assert split_unit("CIGARS", "THNO") == ("CIGARS", "THNO")          # thousands of pieces
    assert split_unit("EXTRA VIRGIN OLIVE OIL", "kg") == ("EXTRA VIRGIN OLIVE OIL", "KG")


def test_expand_abbreviations():
    assert expand("LEATH BUFFAL HIDES,TANN,FINISH") == "leather buffalo hides, tanned, finish"
    assert expand("OTH WOV FAB ART.FIL YARN") == "other woven fabrics artificial filament yarn"
    assert expand("OTHER TUBES & PIPES I/S,NS") == "other tubes and pipes iron/steel, n.e.s."


def test_chapter_specific_meanings():
    assert expand("FR I/NA-S ELECTRO P/C ZINC S-Q", "72").startswith("flat-rolled iron/non-alloy steel")
    assert expand("COD FISH EXCL LVR/ROES FRE/CHL", "03").endswith("livers/roes fresh/chilled")


def test_official_names_and_headings():
    heading, edition = official_heading("29224210", "HS")
    assert edition == "H6" and "glutamic" in heading.lower()
    # the national digits are "00": the code IS the HS6 item -> official text is the name
    c = clean("28151100", "HS", "SODIUM HYDROXIDE SOLID", "KG")
    assert c["display_name"] == "Sodium hydroxide (caustic soda); solid" and c["unit_clean"] == "KG"
    # a national subdivision keeps PBS's specific wording, with the official heading alongside
    c = clean("29224210", "HS", "MONOSODIUM GLUTAMATE", "KG")
    assert c["display_name"] == "Monosodium glutamate" and c["hs_heading"]
    c = clean("0011101", "SITC", "BREEDING BULLS (OXEN)", "NO")
    assert c["display_name"] == "Breeding bulls (oxen)" and c["hs_heading"]
    assert clean("TOTAL", "total", "All products", None)["display_name"] == "All products"


import pytest

from tests.test_api_integration import _db_available, client  # noqa: F401,E402 — fixture


@pytest.mark.skipif(not _db_available(), reason="no database")
def test_products_endpoint_serves_and_searches_readable_names(client):  # noqa: F811
    from app import db
    from app.security import get_redis
    from scripts.load_trade_hs import with_names

    row = with_names([("99991101", "HS", "LEATH BUFFAL HIDES,TANN,FINISH", "SQM", 2020, 2025)])[0]
    db.execute("""INSERT INTO trade_hs_codes (code, classification, description, unit, first_fy, last_fy,
                    display_name, short_name, hs_heading, unit_clean)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (code) DO NOTHING""", row)
    get_redis().delete("cache:v1:trade:hs_codes:v2")
    try:
        r = client.get("/v1/trade/products", params={"q": "buffalo hides"}).json()
        hit = [x for x in r["data"] if x["code"] == "99991101"]
        assert hit and hit[0]["name"] == "Leather buffalo hides, tanned, finish"
        assert hit[0]["description"] == "LEATH BUFFAL HIDES,TANN,FINISH" and hit[0]["unit"] == "SQM"
        assert any(x["code"] == "99991101" for x in client.get("/v1/trade/products", params={"q": "9999"}).json()["data"])
    finally:
        db.execute("DELETE FROM trade_hs_codes WHERE code = '99991101'")
        get_redis().delete("cache:v1:trade:hs_codes:v2")
