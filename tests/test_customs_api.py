"""Customs tariff and valuation rulings: the jobs' upserts and the /v1/trade/tariff + valuation endpoints."""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from tests.test_api_integration import _db_available

fastapi_testclient = pytest.importorskip("fastapi.testclient")
pytestmark = pytest.mark.skipif(not _db_available(), reason="no database reachable")

CODE = "99887711"          # not a real PCT code: the test's rows never collide with loaded data
RULINGS = (990001, 990002)


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return fastapi_testclient.TestClient(app)


@pytest.fixture(scope="module")
def key():
    from app import db
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    row = db.query_one("INSERT INTO users (email, plan, subscription_status) VALUES (%s,'pro','active') RETURNING id",
                       (f"pytest-{uuid.uuid4().hex[:12]}@pakdata.local",))
    db.execute("INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'pytest')", (key_hash, row["id"]))
    yield {"X-API-Key": plaintext}
    db.execute("DELETE FROM api_keys WHERE key_hash=%s", (key_hash,))
    db.execute("DELETE FROM users WHERE id=%s", (row["id"],))


@pytest.fixture(scope="module", autouse=True)
def _data():
    from app import db
    from app.services import cache
    from ingestion.jobs.fbr_customs_tariff import TariffLine, upsert_rows
    from ingestion.jobs.fbr_valuation_rulings import ParsedRuling, Ruling, ValueRow, upsert_all

    def clean():
        db.execute("DELETE FROM customs_tariff WHERE code = %s", (CODE,))
        db.execute("DELETE FROM customs_tariff_duties WHERE code = %s", (CODE,))
        db.execute("DELETE FROM customs_valuation_values WHERE ruling_id = ANY(%s)", (list(RULINGS),))
        db.execute("DELETE FROM customs_valuation_rulings WHERE ruling_id = ANY(%s)", (list(RULINGS),))
        r = cache.get_redis()
        for k in r.scan_iter("cache:v1:trade:tariff:*"):
            r.delete(k)

    clean()
    rows = [TariffLine(2026, CODE, "- - Test widgets", "Widgets. > Test", {"cd": (20.0, None)}),
            TariffLine(2027, CODE, "- - Test widgets", "Widgets. > Test", {"cd": (None, "Rs.10800/MT")}),
            TariffLine(2014, CODE, "- - Test widgets", "Widgets.", {"cd": (5.0, None), "st": (16.0, None),
                                                                   "fta:CN_FTA": (0.0, None)}, "kg")]
    assert upsert_rows(rows) == 3
    assert upsert_rows(rows) == 0                 # unchanged: nothing written (no fake revisions)
    old = Ruling(RULINGS[0], "9001/2025", "Valuation Ruling No. 9001/2025", "ruling",
                 "Determination of Customs Value of Test Widgets under section 25A", "Test Widgets", date(2025, 7, 14), "u1")
    new = Ruling(RULINGS[1], "9002/2026", "Valuation Ruling No. 9002/2026", "ruling",
                 "Determination of Customs Value of Test Widgets under section 25A", "Test Widgets", date(2026, 10, 28), "u2")
    v = lambda origin, value: ValueRow(0, "Test widget", CODE, CODE + "1000", origin, value, "USD", "kg", "C&F")  # noqa: E731
    parsed = [ParsedRuling(RULINGS[0], "text", "parsed", [], [v("China", 1.5)]),
              ParsedRuling(RULINGS[1], "text", "parsed", ["9001/2025"], [v("China", 1.6), v("Other Origins", 1.8)],
                           dated=date(2026, 10, 29))]
    for p in parsed:
        for i, x in enumerate(p.values, 1):
            x.line_no = i
    upsert_all([old, new], parsed)
    yield
    clean()


def test_tariff_endpoint(client, key):
    r = client.get("/v1/trade/tariff", params={"code": f"{CODE[:4]}.{CODE[4:]}"}, headers=key)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert [x["fy"] for x in d] == [2014, 2026, 2027]
    y14, y26, y27 = d
    assert y26["customs_duty_pct"] == 20 and y26["in_force_from"] == "2025-07-01" and y26["fiscal_year"] == "2025-26"
    assert y27["customs_duty_pct"] is None and y27["customs_duty_text"] == "Rs.10800/MT"
    assert y14["duties"]["st"]["rate_pct"] == 16 and "fta:CN_FTA" in y14["duties"] and y14["unit"] == "kg"
    one = client.get("/v1/trade/tariff", params={"code": CODE, "fy": 2026}, headers=key).json()["data"]
    assert [x["fy"] for x in one] == [2026]


def test_tariff_bad_code_and_auth(client, key):
    assert client.get("/v1/trade/tariff", params={"code": "12345"}, headers=key).status_code == 422
    assert client.get("/v1/trade/tariff", params={"code": CODE}).status_code in (401, 403)


def test_valuations_and_supersession(client, key):
    r = client.get("/v1/trade/valuations", params={"code": CODE}, headers=key)
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert len(d) == 3
    newest = [x for x in d if x["ruling"] == "9002/2026"]
    assert {(x["origin"], x["value"]) for x in newest} == {("China", 1.6), ("Other Origins", 1.8)}
    assert newest[0]["issued_on"] == "2026-10-29" and newest[0]["in_force"] is True   # the letter's date wins
    oldest = next(x for x in d if x["ruling"] == "9001/2025")
    assert oldest["in_force"] is False and oldest["superseded_by"] == "9002/2026"
    assert oldest["superseded_on"] == "2026-10-29"
    live = client.get("/v1/trade/valuations", params={"code": CODE, "in_force": "true"}, headers=key).json()["data"]
    assert {x["ruling"] for x in live} == {"9002/2026"}
    assert client.get("/v1/trade/valuations", headers=key).status_code == 422


def test_valuation_rulings_list(client, key):
    r = client.get("/v1/trade/valuation-rulings", params={"q": "Test Widgets"}, headers=key)
    assert r.status_code == 200, r.text
    d = {x["number"]: x for x in r.json()["data"]}
    assert d["9002/2026"]["supersedes"] == ["9001/2025"] and d["9002/2026"]["values_found"] == 2
    assert d["9001/2025"]["superseded_by"] == "9002/2026" and d["9001/2025"]["in_force"] is False


def test_product_page_duty_history():
    from app.api.trade import _duty_history, _valuation_rows

    code = _duty_history(CODE)
    assert [(t["fiscal_year"], t["customs_duty_pct"], t["customs_duty_text"]) for t in code] == [
        ("2013-14", 5, None), ("2025-26", 20, None), ("2026-27", None, "Rs.10800/MT")]
    heading = {t["fy"]: t for t in _duty_history(CODE[:4])}
    assert heading[2026]["min_pct"] == 20 and heading[2027]["specific"] >= 1
    assert {v["origin"] for v in _valuation_rows(CODE[:4], None, None, True, None, None, 25)} >= {"China", "Other Origins"}
