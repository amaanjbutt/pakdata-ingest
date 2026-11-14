"""Karachi Port vessel history parser (ingestion/jobs/kpt_vessel_history.py). Offline.
Fixture: KPT's real Vessel History page for 10 → 14 Nov 2026 (dtEnd is exclusive: arrivals 10–13 Nov), 31
calls in nine category tables — container ships discharging and loading, tankers with the "M.T." prefix
(one with a trailing dot), ships still in port (no departure), arrivals just after midnight ("12:15:00 am")."""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest

from ingestion.framework import FetchedFile
from ingestion.jobs.kpt_vessel_history import KptVesselHistoryJob, page_range, to_rows, vessel_name, when

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "kpt_vessel_history_2026-11-10.html")


def _page() -> bytes:
    with open(FIXTURE, "rb") as fh:
        return fh.read()


def _parse(content: bytes):
    return KptVesselHistoryJob().parse(FetchedFile("kpt_vessel_history_2026-11-10.html", content, date(2026, 11, 14)))


def test_every_call_on_the_page():
    rows = _parse(_page())
    assert len(rows) == 31
    assert {(r.port, r.section) for r in rows} == {("KPT", "calls")}
    assert {r.statement_date for r in rows} == {date(2026, 11, d) for d in (10, 11, 12, 13)}
    assert all(r.arrived_on == r.statement_date for r in rows)
    by = {(r.vessel, r.statement_date): r for r in rows}
    box = by[("DELPHINUS C", date(2026, 11, 10))]  # "12:15:00 am": a quarter past midnight
    assert (box.direction, box.commodity_raw, box.vessel_type) == ("", "Containers", "container")
    assert box.details["arrived_at"] == "2026-11-10T00:15" and box.departs_at == datetime(2026, 11, 11, 9, 45)
    assert box.details["kpt_type"] == "Container Ship" and box.details["work"] == "Disc./Load Containers"
    crude = by[("SHALAMAR", date(2026, 11, 11))]  # "M.T.SHALAMAR"
    assert (crude.direction, crude.commodity_raw, crude.vessel_type) == ("import", "Crude Oil", "tanker")
    assert crude.departs_at == datetime(2026, 11, 13, 10, 40)
    assert ("LAHORE", date(2026, 11, 12)) in by  # "M.T.LAHORE."
    fuel = by[("CLEAN THRASHER", date(2026, 11, 11))]
    assert (fuel.direction, fuel.commodity_raw, fuel.departs_at) == ("export", "Fuel Oil", None)  # still in port
    rice = by[("VS GLORY", date(2026, 11, 12))]
    assert (rice.direction, rice.commodity_raw, rice.details["kpt_type"]) == ("export", "Rice", "Rice")
    assert by[("CETUS ORCA", date(2026, 11, 10))].direction == "import"
    # a ship that came back the next day is a second call
    assert {d for v, d in by if v == "TARLAN"} == {date(2026, 11, 10), date(2026, 11, 11)}
    assert sum(r.departs_at is None for r in rows) == 11


def test_dates_and_names():
    assert when("12/11/2026 12:50:00 pm") == datetime(2026, 11, 12, 12, 50)
    assert when("12/11/2026 12:50:00 am") == datetime(2026, 11, 12, 0, 50)
    assert when("01/05/2026 7:05:00 pm") == datetime(2026, 5, 1, 19, 5)
    assert when("") is None
    with pytest.raises(ValueError):
        when("2026-11-12")
    assert vessel_name("M.T.SARGODHA") == "SARGODHA" and vessel_name("PEARL.") == "PEARL"
    assert vessel_name("MSC TINA") == "MSC TINA" and vessel_name("MTS ALPHA") == "MTS ALPHA"
    assert page_range("From : Aug-14-2026  to Nov-14-2026") == (date(2026, 8, 14), date(2026, 11, 14))


def test_a_call_listed_twice_is_numbered_by_departure():
    """KPT lists a ship that shifted berth twice with one arrival; seq follows the departures (in port last),
    so the numbering doesn't depend on the order the page prints them in."""
    a = datetime(2026, 5, 9, 19, 25)
    item = {"kpt_type": "Container Ship", "vessel": "SELEN", "arrived": a, "purpose": "Disc./Load Containers"}
    for order in ([None, datetime(2026, 5, 11, 3, 0), datetime(2026, 5, 10, 17, 5)],
                  [datetime(2026, 5, 10, 17, 5), None, datetime(2026, 5, 11, 3, 0)]):
        rows = to_rows([{**item, "departed": d} for d in order], "x")
        assert [(r.seq, r.departs_at) for r in rows] == [
            (0, datetime(2026, 5, 10, 17, 5)), (1, datetime(2026, 5, 11, 3, 0)), (2, None)]


def test_vessel_type_trusts_kpts_category():
    a = datetime(2026, 6, 9, 8, 50)
    rows = to_rows([{"kpt_type": t, "vessel": v, "arrived": a, "departed": None, "purpose": w} for t, v, w in (
        ("Tanker", "MARINE XENA", ""), ("Tanker", "PALM STAR", "Discharge Palm Oil"),
        ("Car Carrier", "LEO SPIRIT", "Discharge Package"), ("Rice", "KIAN", "Load Rice"))], "x")
    assert {r.vessel: (r.vessel_type, r.direction, r.commodity_raw) for r in rows} == {
        "MARINE XENA": ("tanker", "", ""), "PALM STAR": ("tanker", "import", "Palm Oil"),
        "LEO SPIRIT": ("bulk_or_general", "import", "Package"), "KIAN": ("bulk_or_general", "export", "Rice")}


def test_an_error_page_or_an_empty_list_fails_the_run():
    with pytest.raises(ValueError, match="no vessel history"):
        _parse(b"<html><body>Something went wrong</body></html>")
    page = _page().decode("utf-8")
    empty = page[:page.find('class="ContainerGrid"')] + 'class="ContainerGrid"></table></body></html>'
    with pytest.raises(ValueError, match="lists no ships"):
        _parse(empty.encode())


def test_a_list_starting_after_the_day_asked_leaves_its_first_day_alone():
    """Asked from 1 Nov, the list starts on 10 Nov: if KPT has started dropping old calls, its first day may
    be cut part-way, so that day keeps what earlier runs stored (the days after are re-read as usual)."""
    rows = _parse(_page().replace(b"Nov-10-2026  to", b"Nov-01-2026  to"))
    assert {r.statement_date for r in rows} == {date(2026, 11, d) for d in (11, 12, 13)}
