"""Karachi Port daily reports parser (ingestion/jobs/kpt_shipping.py). Offline; needs pdfplumber.
Fixtures are KPT's real reports for 31-Oct-2026: the daily tonnage statement (four pages; rows wrap over
several lines and across a page break, container ships carry a TEU line, one ship has two cargoes) and
the container-handling report (TEUs per ship and per terminal, letter-spaced date)."""
from __future__ import annotations

import os
from datetime import date

import pytest

pytest.importorskip("pdfplumber")

from ingestion.framework import FetchedFile  # noqa: E402
from ingestion.jobs.kpt_shipping import KptShippingJob, report_date  # noqa: E402
from ingestion.port_calls import VesselRow  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _parse(fixture: str, filename: str):
    with open(os.path.join(FIXTURES, fixture), "rb") as fh:
        out = KptShippingJob().parse(FetchedFile(filename, fh.read(), date(2026, 10, 31)))
    rows = [r for r in out if isinstance(r, VesselRow)]
    recs = {(r.series_id, tuple(sorted(r.dims.items()))): r.value for r in out if not isinstance(r, VesselRow)}
    return rows, recs


def _ship(rows, vessel, direction):
    return next(r for r in rows if r.vessel == vessel and r.direction == direction)


def test_tonnage_statement_ships_and_figures():
    rows, recs = _parse("kpt_shipcargo_2026-10-31.pdf", "kpt_shipcargo.pdf")
    assert {r.statement_date for r in rows} == {date(2026, 10, 31)}
    crude = _ship(rows, "Mardan", "import")
    assert (crude.berth_raw, crude.commodity_raw, crude.handled_24h, crude.balance, crude.manifest) == \
        ("OP-1", "Crude Oil", 25900, 39258, 65158)
    assert crude.details["grt"] == 58168 and crude.details["commenced"] == "2026-10-30"
    clinker = _ship(rows, "Rubaiyat Hanif", "export")
    assert (clinker.handled_24h, clinker.balance) == (7500, 41840)
    # a container ship's TEU line; a ship discharging and loading
    benin_in, benin_out = _ship(rows, "Msc Benin", "import"), _ship(rows, "Msc Benin", "export")
    assert (benin_in.details["teu"], benin_in.details["teu_24h"]) == (887, 59)
    assert (benin_out.handled_24h, benin_out.details["teu_24h"]) == (10091, 1036)
    # two cargoes in one ship
    assert sorted(r.commodity_raw for r in rows if r.vessel == "Mozamel") == ["General Cargo", "Rice"]
    # the row that runs across the page break keeps its name; the closing summary isn't a ship
    assert any(r.vessel == "Laem Chabang Voyager" for r in rows)
    assert not any("SUMMARY" in (r.agent_raw or "") or "Wharf Wise" in (r.agent_raw or "") for r in rows)
    assert all(r.checks_ok for r in rows)  # both wharves add up to their printed totals
    # the West Wharf, after the East Wharf's total line, is read too
    assert {r.details.get("wharf") for r in rows} == {"East Wharf", "West Wharf"}
    assert _ship(rows, "Wan Hai 612", "import").details["wharf"] == "West Wharf"
    total = recs[("ports.karachi.cargo_handled", (("direction", "all"), ("level", "total")))]
    assert total == sum(r.handled_24h for r in rows if r.direction in ("import", "export"))  # = GRAND TOTAL
    assert recs[("ports.karachi.ships_working", (("level", "total"),))] == len({(r.vessel, r.berth_raw) for r in rows})


def test_statement_summary_by_cargo_type():
    rows, recs = _parse("kpt_shipcargo_2026-11-10.pdf", "kpt_shipcargo.pdf")
    key = lambda d, c=None: ("ports.karachi.cargo_handled", tuple(sorted(  # noqa: E731
        {"level": "cargo_type" if c else "total", "direction": d, **({"cargo": c} if c else {})}.items())))
    # SUMMARY: containers 54,872 / 35,941; general 0 / 573; bulk 18,801 / 8,663; liquid 30,824 / 0
    assert (recs[key("import")], recs[key("export")], recs[key("all")]) == (104497, 45177, 149674)
    assert recs[key("import", "containers")] == 54872 and recs[key("export", "break_bulk")] == 573
    assert recs[key("export", "general")] == 35941 + 573  # general cargo includes containers, as KPT counts it
    assert recs[key("import", "dry")] == 54872 + 18801 and recs[key("import", "liquid")] == 30824
    assert {r.details.get("wharf") for r in rows} == {"East Wharf", "West Wharf"} and all(r.checks_ok for r in rows)


def test_shipping_programme():
    rows, recs = _parse("kpt_pplan_2026-11-10.pdf", "kpt_pplan.pdf")
    assert {r.statement_date for r in rows} == {date(2026, 11, 10)} and {r.section for r in rows} == {"programme"}
    wheat = next(r for r in rows if r.vessel == "Caravos Liberty")
    assert (wheat.vessel_type, wheat.direction, wheat.commodity, wheat.manifest, wheat.call_ref) == \
        ("Wheat", "import", "Wheat", 58488, "2910260011/06")
    assert wheat.loa_m == 200.0 and wheat.draft_m == 12.9 and wheat.expected_at.isoformat() == "2026-11-06T10:42:00"
    crude = next(r for r in rows if r.vessel == "Shalamar")  # 'M.T.Shalamar'
    assert (crude.commodity, crude.manifest, crude.berth_raw, crude.terminal) == ("Crude oil", 73000, "Op-1", "Oil Piers")
    peas = {r.commodity: r.manifest for r in rows if r.vessel == "Jing Dian Chen Guang"}
    assert peas == {"Yellow peas": 22080, "Lentils": 5500}  # a second cargo on its own line
    cars = next(r for r in rows if r.vessel == "Deneb Leader")
    assert (cars.unit, cars.manifest) == ("units", 124)
    box = next(r for r in rows if r.vessel == "Kota Plumbago")
    assert box.manifest is None and box.terminal == "South Asia Pakistan Terminals (SAPT)"
    sched = recs[("ports.karachi.cargo_scheduled", (("direction", "import"), ("level", "total")))]
    assert sched == sum(r.manifest for r in rows if r.direction == "import" and r.unit == "metric ton")
    n = recs[("ports.karachi.ships_scheduled", (("direction", "all"), ("level", "total")))]
    assert n == len({r.vessel for r in rows})


def test_tonnage_report_history():
    from ingestion.jobs.kpt_tonnage_history import figures, to_records

    with open(os.path.join(FIXTURES, "kpt_tonnage_report_2026-11-09.html"), encoding="utf-8") as fh:
        nums = figures(fh.read())
    assert nums[:5] == [0.019, 0.055, 0.074, 0.031, 0.104] and nums[15:] == [0.004, 0.005]
    recs = {(r.series_id, tuple(sorted(r.dims.items()))): (r.obs_date, r.value)
            for r in to_records(date(2026, 11, 10), nums, monthly=False)}
    # the report's 9 November is the statement of the 10th (24 hours to 07:30): 104,497 t imported, to the 1,000 t
    cargo, teu = "ports.karachi.cargo_handled", "ports.karachi.containers_teu"
    assert recs[(cargo, (("direction", "import"), ("level", "total")))] == (date(2026, 11, 10), 104000)
    assert recs[(cargo, (("cargo", "bulk"), ("direction", "export"), ("level", "cargo_type")))][1] == 9000
    assert recs[(teu, (("cargo", "all"), ("direction", "all"), ("level", "total")))][1] == 9000


def test_tonnage_report_day_without_bulk_cargo():
    from ingestion.jobs.kpt_tonnage_history import figures, to_records

    with open(os.path.join(FIXTURES, "kpt_tonnage_report_2022-12-05.html"), encoding="utf-8") as fh:
        nums = figures(fh.read())  # no "Bulk Cargo" rows that day: those figures are left out, not shifted
    assert nums[0] is None and nums[1:5] == [0.009, 0.009, 0.014, 0.023] and nums[15:] == [0.001, 0.0]
    recs = {tuple(sorted(r.dims.items())): r.value for r in to_records(date(2022, 12, 6), nums, monthly=False)
            if r.series_id == "ports.karachi.cargo_handled"}
    assert recs[(("direction", "import"), ("level", "total"))] == 23000
    assert (("cargo", "bulk"), ("direction", "import"), ("level", "cargo_type")) not in recs
    assert figures("<html>Service unavailable</html>") is None


def test_container_report_teus_and_terminals():
    rows, recs = _parse("kpt_tues_2026-10-31.pdf", "kpt_tues.pdf")
    breeze = _ship(rows, "Seaspan Breeze", "export")
    assert (breeze.berth_raw, breeze.handled_24h, breeze.details["tonnes"], breeze.unit) == ("Sapt-2", 704, 6848, "teu")
    assert all(r.checks_ok for r in rows)  # laden + empty boxes add up to the TEUs
    teu = "ports.karachi.containers_teu"
    assert recs[(teu, (("cargo", "all"), ("direction", "all"), ("level", "total")))] == 6628  # GRAND TOTAL
    assert recs[(teu, (("cargo", "laden"), ("direction", "import"), ("level", "total")))] == 288 + 100 + 3051


def test_report_date_letter_spaced():
    assert report_date("C O N T A I N E R H A N D L I N G\nDuring 24 Hrs. Ending 0700 Hrs. on 3 1 - O C T - 2 0 2 6") \
        == date(2026, 10, 31)
    assert report_date("During 24 Hrs. ending at 0730 Hrs. on 31-OCT-2026\nPrinted on: 01-NOV-2026") == date(2026, 10, 31)


def test_reports_still_in_progress_and_tomorrows_programme(monkeypatch):
    from datetime import datetime as real_datetime

    from ingestion.jobs import kpt_shipping

    class Night(real_datetime):  # 01:00 on the 10th in Karachi: that day's tonnage report is still adding up
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 10, 1, 0, tzinfo=kpt_shipping.PKT)

    class Eve(date):  # the evening before: the programme printed for the 10th
        @classmethod
        def today(cls):
            return date(2026, 11, 9)

    monkeypatch.setattr(kpt_shipping, "datetime", Night)
    rows, recs = _parse("kpt_shipcargo_2026-11-10.pdf", "kpt_shipcargo.pdf")
    assert rows == [] and recs == {}
    monkeypatch.setattr(kpt_shipping, "date", Eve)
    rows, recs = _parse("kpt_pplan_2026-11-10.pdf", "kpt_pplan.pdf")
    assert rows and recs == {}  # ships kept, series wait for the day
