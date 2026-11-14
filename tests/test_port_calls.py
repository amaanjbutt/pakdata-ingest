"""The ports vessel table (ingestion/port_calls.py) and /v1/ports. Needs Postgres (skipped without it)."""
from __future__ import annotations

from datetime import date

import pytest

from ingestion.port_calls import VesselRow, number_seqs, upsert_rows
from tests.test_api_integration import _db_available, api_key, client  # noqa: F401 — shared fixtures

needs_db = pytest.mark.skipif(not _db_available(), reason="no database reachable")
PORT = "ZZT"  # a port code no job writes


def _row(day, vessel, direction="import", balance=100.0, **kw):
    return VesselRow(port=PORT, statement_date=day, section="berth", vessel=vessel, call_ref="9", direction=direction,
                     commodity_raw="COAL", commodity="Coal", unit="metric ton", manifest=500.0, handled_24h=50.0,
                     handled_before=350.0, balance=balance, checks_ok=True, **kw)


@pytest.fixture
def clean():
    from app import db

    db.execute("DELETE FROM port_vessel_calls WHERE port = %s", (PORT,))
    yield db
    db.execute("DELETE FROM port_vessel_calls WHERE port = %s", (PORT,))


def test_number_seqs_tells_parcels_apart():
    d = date(2026, 10, 1)
    rows = number_seqs([_row(d, "A"), _row(d, "A"), _row(d, "B")])
    assert [r.seq for r in rows] == [0, 1, 0]


@needs_db
def test_a_statement_replaces_its_day_and_keeps_other_days(clean):
    db = clean
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    upsert_rows([_row(d1, "A"), _row(d1, "B"), _row(d2, "A", balance=50.0)])
    first = db.query_one("SELECT first_seen_at, updated_at FROM port_vessel_calls WHERE port=%s AND vessel='A' "
                         "AND statement_date=%s", (PORT, d1))
    # the same statement again: nothing changes
    upsert_rows([_row(d1, "A"), _row(d1, "B")])
    again = db.query_one("SELECT first_seen_at, updated_at FROM port_vessel_calls WHERE port=%s AND vessel='A' "
                         "AND statement_date=%s", (PORT, d1))
    assert again == first
    # a re-upload of day 1 that restates A's balance and no longer lists B
    upsert_rows([_row(d1, "A", balance=90.0)])
    got = db.query("SELECT vessel, statement_date, balance FROM port_vessel_calls WHERE port=%s "
                   "ORDER BY statement_date, vessel", (PORT,))
    assert [(r["vessel"], r["statement_date"], float(r["balance"])) for r in got] == [("A", d1, 90.0), ("A", d2, 50.0)]


@needs_db
def test_two_jobs_on_one_port_each_replace_only_their_own_sections(clean):
    """Karachi: kpt_shipping writes berth/containers/programme rows, kpt_vessel_history 'calls' rows, both
    dated by day; each run replaces only its own sections of a day."""
    db = clean
    d = date(2026, 11, 12)

    def call(vessel):
        return VesselRow(port=PORT, statement_date=d, section="calls", vessel=vessel, arrived_on=d)

    upsert_rows([_row(d, "A"), _row(d, "B")], sections=("berth",))
    upsert_rows([call("A"), call("C")], sections=("calls",))
    upsert_rows([_row(d, "A")], sections=("berth",))   # B left the berth: removed, the calls stay
    upsert_rows([call("C")], sections=("calls",))      # A's call dropped: removed, the berth row stays
    got = db.query("SELECT section, vessel FROM port_vessel_calls WHERE port=%s ORDER BY section, vessel", (PORT,))
    assert [(r["section"], r["vessel"]) for r in got] == [("berth", "A"), ("calls", "C")]
    with pytest.raises(ValueError):
        upsert_rows([call("D")], sections=("berth",))


@needs_db
def test_ports_api(client, api_key, clean):  # noqa: F811
    d1, d2 = date(2026, 10, 1), date(2026, 10, 2)
    upsert_rows([_row(d1, "ALPHA STAR"), _row(d1, "BETA"), _row(d2, "ALPHA STAR", balance=50.0)])
    h = {"X-API-Key": api_key}
    assert client.get("/v1/ports/vessels", params={"port": PORT}, headers=h).status_code == 422  # PQA | KPT only
    # one ship across statements, newest first, any port
    body = client.get("/v1/ports/vessels", params={"vessel": "alpha star"}, headers=h).json()
    mine = [x for x in body["data"] if x["port"] == PORT]
    assert [(x["date"], x["balance"], x["commodity"]) for x in mine] == [("2026-10-02", 50.0, "Coal"),
                                                                        ("2026-10-01", 100.0, "Coal")]
    csv = client.get("/v1/ports/vessels", params={"vessel": "alpha star", "format": "csv"}, headers=h)
    assert csv.text.splitlines()[0].startswith("date,port,section,vessel,call_ref,direction,commodity")
    assert client.get("/v1/ports/vessels", params={"vessel": "alpha star"}).status_code == 401
    assert client.get("/v1/ports/dates", params={"port": "PQA"}).status_code == 200
