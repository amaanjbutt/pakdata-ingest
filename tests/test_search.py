"""Site search: series (every word must match) + typed entities (search_entities)."""
from __future__ import annotations

import pytest

from tests.test_api_integration import _db_available, client  # noqa: F401 — fixture

needs_db = pytest.mark.skipif(not _db_available(), reason="no database")

ROWS = [
    ("trade_product", "99991101", "Leather buffalo hides, tanned, finish", "Traded product · HS 99991101",
     "/trade?code=99991101", 2.0, "leather buffalo hides, tanned, finish leath buffal hides,tann,finish"),
    ("district", "Punjab|Testpur", "Testpur", "District, Punjab · population 1,000",
     "/districts/testpur", 4.0, "testpur punjab district population census literacy profile"),
    ("plant", "Testkarot", "Testkarot", "Power plant · Hydel", "/series/energy.generation.plant", 3.0,
     "testkarot hydel hps testkarot 720mw power plant"),
    ("mandi_crop", "testonion", "Testonion — mandi rates", "Wholesale prices · Punjab and Sindh",
     "/mandi/testonion", 4.0, "testonion mandi wholesale price rate"),
    ("mandi_market", "sindh_testkarachi", "Testkarachi mandi", "Wholesale market · Sindh",
     "/mandi?market=sindh_testkarachi&province=sindh", 2.5, "testkarachi sindh mandi market wholesale"),
]


@pytest.fixture
def entities():
    from app import db

    db.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for r in ROWS:
        db.execute("""INSERT INTO search_entities (kind, key, title, subtitle, url, boost, haystack)
                      VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (kind, key) DO NOTHING""", r)
    yield
    for r in ROWS:
        db.execute("DELETE FROM search_entities WHERE kind = %s AND key = %s", (r[0], r[1]))


@needs_db
def test_default_is_series_only_for_old_clients(client, entities):  # noqa: F811
    body = client.get("/v1/search", params={"q": "testpur"}).json()
    assert body["success"] and all("id" in r for r in body["data"])        # Sheets PAKDATA_SEARCH shape
    assert not any(r.get("kind") == "district" for r in body["data"])


@needs_db
def test_kinds_all_finds_entities_by_any_name(client, entities):  # noqa: F811
    def first(q):
        data = client.get("/v1/search", params={"q": q, "kinds": "all"}).json()["data"]
        return data[0] if data else None

    assert first("buffalo hides")["url"] == "/trade?code=99991101"         # expanded PBS abbreviation
    assert first("leath buffal")["kind"] == "trade_product"                 # raw PBS text still works
    assert first("testpur population")["url"] == "/districts/testpur"       # words across name + keywords
    assert first("testkarot")["kind"] == "plant"
    assert first("testkarote")["kind"] == "plant"                           # typo -> trigram fallback
    pair = first("testonion testkarachi")
    assert pair["url"] == "/mandi/testonion?market=sindh_testkarachi" and pair["title"].startswith("Testonion in")


@needs_db
def test_kinds_filter_and_validation(client, entities):  # noqa: F811
    data = client.get("/v1/search", params={"q": "test", "kinds": "plant,district"}).json()["data"]
    assert data and {r["kind"] for r in data} <= {"plant", "district"}
    assert client.get("/v1/search", params={"q": "x", "kinds": "planets"}).status_code == 422


@needs_db
def test_series_search_needs_every_word(client):  # noqa: F811
    from app import db

    db.execute("""INSERT INTO series (id, module, name, unit, frequency, source, is_active)
                  VALUES ('test.search.kibor_3m', 'fixed-income', 'Testkibor three month offer', 'percent',
                          'daily', 'SBP', true) ON CONFLICT (id) DO NOTHING""")
    try:
        hit = client.get("/v1/search", params={"q": "testkibor offer"}).json()["data"]
        assert [r["id"] for r in hit] == ["test.search.kibor_3m"]
        assert client.get("/v1/search", params={"q": "testkibor bid"}).json()["data"] == []
    finally:
        db.execute("DELETE FROM series WHERE id = 'test.search.kibor_3m'")


def test_refresh_builders_cover_every_kind():
    from app.services.search import ENTITY_KINDS
    from ingestion import search_entities

    assert set(search_entities.KINDS) == set(ENTITY_KINDS)
