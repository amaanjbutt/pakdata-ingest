"""fbr_sales_tax_schedules: the Sales Tax Act's Sixth and Eighth Schedules, parsed from trimmed pages of the real
editions (as amended up to 30 June 2026 and 1 July 2015) and the whole July 2014 Word file; then the upsert and
the /v1/trade/sales-tax endpoints."""
from __future__ import annotations

import os
import uuid
from collections import Counter

import pytest

from ingestion.jobs.fbr_sales_tax_schedules import (
    Entry,
    Item,
    _footnote_marks,
    _key,
    clean,
    expand_codes,
    is_omitted,
    norm_serial,
    parse_file,
    printed_codes,
    rate_pct,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _parse(fn: str, fy: int, as_of: str):
    with open(os.path.join(FIX, fn), "rb") as f:
        return parse_file(f.read(), fy, as_of, "u")


@pytest.fixture(scope="module")
def ed2026():
    return _parse("fbr_sales_tax_act_2026-27_pages.pdf", 2027, "2026-06-30")


def _by(entries):
    return {(e.schedule, e.tbl, e.serial): e for e in entries}


# ---- helpers ---------------------------------------------------------------------------------------

def test_text_helpers():
    assert clean("Live Animals \x02[and live poultry.]  ") == "Live Animals and live poultry."
    assert clean("1[151. (a) Supplies") == "151. (a) Supplies"           # a footnote number printed full size
    assert clean("non-\ntoxic bags") == "non-toxic bags"
    assert norm_serial("[ 182") == "182" and norm_serial("08.") == "8" and norm_serial("52-A") == "52A"
    assert norm_serial("a 117.") == "117"
    assert rate_pct("[10%].") == 10.0 and rate_pct("10%.”; and") == 10.0 and rate_pct("12.75%") == 12.75
    assert rate_pct("Rs. 700 per metric tonne or 18% ad valorem") is None and rate_pct("8.5%. 12.75%") is None
    assert is_omitted(Entry(1, "", "sixth", "1", 0, "4", [Item("***] omitted")]))
    assert is_omitted(Entry(1, "", "sixth", "1", 0, "4", [Item("(****)", "(****)")]))
    # an omitted serial takes the items still printed under it
    assert is_omitted(Entry(1, "", "eighth", "1", 0, "53", [Item("*** Omitted"), Item("(i) Projector", "9007.2000")]))
    assert not is_omitted(Entry(1, "", "eighth", "1", 0, "84", [Item("", "3213.1000")]))


def test_printed_codes():
    toks = printed_codes("0713.1000, 0713.2010 and 49.01, 9937, 1209. 9130, 160 2.5000, 30 June, 2018, "
                         "9917(2), 8432.1010 to 8432.9090 and Respective headings")
    assert toks == [("code", "07131000"), ("code", "07132010"), ("heading", "4901"), ("heading", "9937"),
                    ("code", "12099130"), ("code", "16025000"), ("part", "9917(2)"), ("range", "84321010-84329090")]
    assert printed_codes("713.2010") == []                  # a code missing its first digit is not guessed
    assert printed_codes("30.06.2026") == []                # a date is not a heading
    assert printed_codes("Respective headings") == []


def test_footnote_marks_only_drop_small_numbers_before_a_bracket_or_body_text():
    def ch(text, size, x):
        return {"text": text, "size": size, "x0": x, "top": 0.0}
    # "807[The": a 6pt number before the bracket; "[10%]": an amendment printed small, kept
    chars = [ch(t, 6, i) for i, t in enumerate("807")] + [ch("[", 10, 3), ch("T", 10, 4)] + \
        [ch(t, 6, 10 + i) for i, t in enumerate("[10%]")] + [ch("8", 6, 20), ch("1", 10, 21)]
    drop = _footnote_marks(chars, 8.0)
    kept = "".join(c["text"] for c in chars if _key(c) not in drop)
    assert kept == "[T[10%]1"


# ---- the 2026 edition (trimmed) --------------------------------------------------------------------

def test_sixth_schedule_table1(ed2026):
    entries, skipped = ed2026
    by = _by(entries)
    assert Counter((e.schedule, e.tbl) for e in entries) == {("sixth", "1"): 18, ("sixth", "2"): 34, ("sixth", "3"): 4,
                                                            ("sixth", "4"): 37, ("eighth", "1"): 24}
    assert skipped["omitted"] > 100 and not [k for k in skipped if k != "omitted"]
    pulses = by[("sixth", "1", "14")]
    assert pulses.description == "Pulses."
    assert pulses.codes_text.startswith("0713.1000, 0713.2010, 0713.2020, 0713.2090,")   # "811[0713.2010": marker gone
    assert ("sixth", "1", "1") not in by and ("sixth", "1", "27A") in by        # S.1 omitted; 27A read
    assert by[("sixth", "1", "17")].codes_text == "09.10"
    assert by[("sixth", "1", "143")].codes_text == "9937"
    assert all(e.fy == 2027 and e.as_of == "2026-06-30" for e in entries)
    assert [e.seq for e in entries if (e.schedule, e.tbl) == ("sixth", "1")] == list(range(1, 19))


def test_items_and_page_breaks(ed2026):
    pest = _by(ed2026[0])[("sixth", "1", "133")]
    texts = [i.desc for i in pest.items]
    assert texts[0].startswith("Pesticides and their active ingredients")
    assert ("Toluene", "2902.3000") in [(i.desc, i.codes) for i in pest.items]
    # a page that starts with a new item ("Dioctyl orthophthalates") is not glued to the page before
    assert ("Dioctyl orthophthalates", "2917.3200") in [(i.desc, i.codes) for i in pest.items]
    assert ".." not in texts                                                   # "[..]" rows dropped
    # Table-4's list prints each page's first row above the drawn table (no top border): still read
    t4 = [e for e in ed2026[0] if (e.schedule, e.tbl) == ("sixth", "4")]
    assert [e.serial for e in t4] == [str(i) for i in range(1, 38)]
    assert (t4[6].description, t4[6].codes_text) == ("Cucumbers and gherkins fresh or chilled", "0707.0000")
    parts = _by(ed2026[0])[("sixth", "3", "18")]
    assert parts.items[1].desc == "(i) Bare PCBs" and parts.items[1].codes == "8534.0000"
    assert parts.condition.startswith("If imported by manufacturers and assemblers of computers")


def test_eighth_schedule(ed2026):
    by = _by(ed2026[0])
    assert by[("eighth", "1", "23")].rate_text == "5%" and by[("eighth", "1", "23")].rate_pct == 5.0
    coal = by[("eighth", "1", "47")]
    assert coal.rate_text == "Rs. 700 per metric tonne or 18% ad valorem, whichever is higher" and coal.rate_pct is None
    assert coal.codes_text == "27.01"
    ev = by[("eighth", "1", "70")]                 # a 7-column page: codes and rate under split header cells
    assert ev.codes_text == "Respective Heading" and ev.rate_text == "1%" and ev.condition == "Local supplies only"
    assert len(ev.items) == 7 and ev.items[1].desc.startswith("(i) Road Tractors")
    stationery = by[("eighth", "1", "84")]         # one rate printed beside its second item covers all seven
    assert stationery.rate_text == "10%" and len(stationery.items) == 7
    assert ("ninth", "1") not in Counter((e.schedule, e.tbl) for e in ed2026[0])


# ---- the 1 July 2015 edition (trimmed): headerless pages, a 4-column page, overlaid text --------------

def test_2015_edition():
    entries, skipped = _parse("fbr_sales_tax_act_2015-16_pages.pdf", 2016, "2015-07-01")
    by = _by(entries)
    assert skipped["unreadable (overlaid text)"] == 1                 # Table-2 S.11, struck-out and new text overlaid
    assert ("sixth", "2", "11") not in by
    marble = by[("sixth", "2", "18")]               # description in the 4-column page's third physical column
    assert marble.description.startswith("Supplies made by manufacturers of marble and granite having annual")
    assert marble.codes_text == "Respective headings"
    assert by[("sixth", "2", "19")].codes_text == "6901.1000"
    assert by[("eighth", "1", "1")].rate_text == "10%" and by[("eighth", "1", "1")].codes_text == "2304.0000"
    assert by[("eighth", "1", "15")].rate_text == "5%"


# ---- the July 2014 Word file -----------------------------------------------------------------------

def test_2014_word_edition():
    entries, skipped = _parse("fbr_sales_tax_act_2014-07.doc", 2015, "2014-07-01")
    by = _by(entries)
    counts = Counter((e.schedule, e.tbl) for e in entries)
    assert counts[("sixth", "1")] == 94 and counts[("eighth", "1")] == 6 and counts[("eighth", "2")] == 7
    live = by[("sixth", "1", "1")]
    assert live.description == "Live Animals and live poultry." and "0101.2100" in live.codes_text
    # the serial cell repeating its row's text still starts a new entry
    assert by[("sixth", "2", "11")].codes_text == "0701.9000 and 0703.1000"
    assert len(by[("sixth", "2", "10")].items) == 1
    # Eighth Schedule Table-2 prints its rate once, above the table
    assert {e.rate_text for e in entries if (e.schedule, e.tbl) == ("eighth", "2")} == {"5%"}
    assert by[("eighth", "1", "1")].rate_text == "5%" and by[("eighth", "1", "1")].codes_text == "2304.0000"


# ---- per-code expansion ----------------------------------------------------------------------------

def test_expand_codes():
    e = Entry(2027, "2026-06-30", "eighth", "1", 4, "73", [
        Item("Hybrid vehicles:"), Item("(b) From 1801 cc", "87.03, 8704.9999", "12.75%"), Item("(c) parts", "9917(2)")])
    s = Entry(2027, "2026-06-30", "sixth", "1", 1, "14", [Item("Pulses.", "0713.1000 and Respective headings")])
    tariff = {2027: ["07131000", "87032100", "87039000", "87041000"]}
    rows, rejects = expand_codes([e, s], tariff)
    got = {(r.seq, r.code, r.printed, r.item, r.rate_pct) for r in rows}
    assert got == {(4, "87032100", "87.03", "(b) From 1801 cc", 12.75), (4, "87039000", "87.03", "(b) From 1801 cc", 12.75),
                   (1, "07131000", "0713.1000", None, None)}
    assert rejects == ["FY2026-27 eighth T1 S.73: 8704.9999"]
    # no tariff loaded for the year: printed 8-digit codes only, nothing rejected
    rows, rejects = expand_codes([s], {})
    assert [(r.code, r.printed) for r in rows] == [("07131000", "0713.1000")] and rejects == []


# ---- upsert + API ----------------------------------------------------------------------------------

from tests.test_api_integration import _db_available  # noqa: E402

FY = 2099                  # a far-future edition: the test's rows never collide with loaded data
CODE = "97997722"


@pytest.fixture(scope="module")
def loaded():
    if not _db_available():
        pytest.skip("no database reachable")
    from app import db
    from app.services import cache
    from ingestion.jobs.fbr_sales_tax_schedules import upsert_rows

    db.execute("DELETE FROM sales_tax_schedules WHERE fy = %s", (FY,))
    db.execute("DELETE FROM customs_tariff WHERE code = %s AND fy = %s", (CODE, FY))
    entries = [
        Entry(FY, "2098-06-30", "sixth", "1", 1, "14", [Item("Test pulses.", f"{CODE[:4]}.{CODE[4:]}")], "u"),
        Entry(FY, "2098-06-30", "eighth", "1", 1, "23", [Item("Test clothing", f"{CODE[:2]}.{CODE[2:4]}", "5%", "If worn")], "u"),
        Entry(FY, "2098-06-30", "sixth", "2", 1, "3", [Item("Supplies made by cottage industry.", "Respective headings.")], "u"),
    ]
    codes, rejects = expand_codes(entries, {FY: [CODE]})
    assert rejects == []
    assert upsert_rows(entries, codes) == 3 + 2
    assert upsert_rows(entries, codes) == 0          # unchanged edition: nothing written
    r = cache.get_redis()
    for k in r.scan_iter("cache:v1:trade:*"):
        r.delete(k)
    yield
    db.execute("DELETE FROM sales_tax_schedules WHERE fy = %s", (FY,))


@pytest.fixture(scope="module")
def api():
    from fastapi.testclient import TestClient

    from app import db
    from app.main import app
    from app.security import generate_key

    plaintext, key_hash = generate_key()
    row = db.query_one("INSERT INTO users (email, plan, subscription_status) VALUES (%s,'business','active') RETURNING id",
                       (f"pytest-{uuid.uuid4().hex[:12]}@pakdata.local",))
    db.execute("INSERT INTO api_keys (key_hash, user_id, label) VALUES (%s,%s,'pytest')", (key_hash, row["id"]))
    yield TestClient(app), {"X-API-Key": plaintext}
    db.execute("DELETE FROM api_keys WHERE key_hash=%s", (key_hash,))
    db.execute("DELETE FROM users WHERE id=%s", (row["id"],))


def test_sales_tax_endpoints(loaded, api):
    client, key = api
    r = client.get("/v1/trade/sales-tax", params={"code": CODE}, headers=key)
    assert r.status_code == 200
    rows = r.json()["data"]
    assert [(x["schedule"], x["serial"], x["printed"], x["rate_pct"]) for x in rows] == [
        ("sixth", "14", "9799.7722", None), ("eighth", "23", "97.99", 5.0)]
    assert rows[1]["condition"] == "If worn" and rows[1]["as_of"] == "2098-06-30" and rows[1]["fiscal_year"] == "2098-99"
    sched = client.get("/v1/trade/sales-tax-schedules", params={"fy": FY, "schedule": "sixth"}, headers=key).json()
    assert [(x["table"], x["serial"], x["codes"]) for x in sched["data"]] == [("1", "14", [CODE]), ("2", "3", [])]
    assert sched["meta"]["editions"][-1] == {"fy": FY, "fiscal_year": "2098-99", "as_of": "2098-06-30",
                                             "entries": {"sixth": 2, "eighth": 1}}
    assert client.get("/v1/trade/sales-tax", params={"code": "123"}, headers=key).status_code == 422
    assert client.get("/v1/trade/sales-tax", params={"code": CODE}).status_code in (401, 403)


def test_product_page_sales_tax_section(loaded):
    from app.api.trade import _sales_tax_page

    st = _sales_tax_page(CODE)
    assert st["latest_fy"] == FY
    assert [(e["schedule"], e["serial"], e["fiscal_years"], e["in_latest"]) for e in st["entries"]] == [
        ("sixth", "14", ["2098-99"], True), ("eighth", "23", ["2098-99"], True)]
