"""Port Qasim daily statement parser (ingestion/jobs/pqa_shipping.py). Offline; needs pdfplumber.
Fixtures are real signed PQA statements: 31-Oct-2026 (current layout: two ships sharing the PIBT berth,
container ships both ways, a terminal-first anchorage table, previous day's sailings, no ships expected),
21-Mar-2026 (nobody waiting at the outer anchorage) and 16-Jul-2019 (the oldest layout: a tanker with two
cargoes on one berth, containers printed without a manifest, an L.O.A-first anchorage table, a ship
expected)."""
from __future__ import annotations

import os
from datetime import date, datetime

import pytest

pytest.importorskip("pdfplumber")

from ingestion.framework import FetchedFile  # noqa: E402
from ingestion.jobs.pqa_shipping import (  # noqa: E402
    PqaShippingJob, SubRow, album_links, assign, checked_date, figure, last_page, parse_pdf, statement_date,
    sub_rows, to_records, vessel_name,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _parse(name: str):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return parse_pdf(fh.read(), name)


def _rows(st, section, vessel):
    return [r for r in st.rows if r.section == section and r.vessel == vessel]


def test_current_layout_berths_two_ships_and_directions():
    st = _parse("pqa_statement_2026-10-31.pdf")
    assert st.day == date(2026, 10, 31)
    # two ships share the PIBT cell: split on their registration numbers
    fin, = _rows(st, "berth", "AFRICAN FINFOOT")
    cri, = _rows(st, "berth", "STAR CRIMSON")
    assert (fin.berth_raw, fin.call_ref, fin.commodity, fin.direction) == ("PIBT", "38033", "Petroleum coke", "import")
    assert (fin.manifest, fin.handled_24h, fin.handled_before, fin.balance, fin.checks_ok) == (51717, 1946, 46399, 3372, True)
    assert (cri.call_ref, cri.commodity, cri.manifest, cri.handled_24h, cri.balance) == ("38057", "Coal", 59674, 23500, 36174)
    assert fin.berthed_at == datetime(2026, 10, 20, 9, 45) and cri.agent == "Ocean World"
    # rice is loaded: its figures sit on the commodity's second line
    rice, = _rows(st, "berth", "MAGIC CELESTE")
    assert (rice.direction, rice.manifest, rice.handled_24h, rice.handled_before, rice.balance) == \
        ("export", 40105, 6053, 957, 33095)
    # a container ship fills both lines
    box = {r.direction: r for r in _rows(st, "berth", "BSG BIMINI")}
    assert (box["import"].manifest, box["import"].handled_24h, box["export"].manifest, box["export"].handled_24h) == \
        (2765, 65, 2457, 1877)
    assert box["import"].unit == "teu" and box["import"].departs_at == datetime(2026, 10, 30, 21, 55)
    # 'NO WORK' reads as 0 with a note; a ship berthed in June keeps June's year
    bit, = _rows(st, "berth", "OCEAN PRINCESS-1")
    assert bit.handled_24h == 0 and bit.details["notes"] == ["no work"] and bit.berthed_at == datetime(2026, 6, 28, 7, 0)
    lng = [r for r in st.rows if r.section == "berth" and r.berth_raw == "EETL"]
    assert lng == []  # vacant
    assert all(r.checks_ok is not False for r in st.rows if r.section == "berth")


def test_current_layout_anchorage_sailings_and_berthed_counts():
    st = _parse("pqa_statement_2026-10-31.pdf")
    anch = [r for r in st.rows if r.section == "anchorage"]
    assert len({r.vessel for r in anch}) == 22
    edge, = _rows(st, "anchorage", "EDGE GALAXY")
    assert (edge.call_ref, edge.commodity, edge.manifest, edge.direction) == ("38035", "Palm oil", 18800, "import")
    assert (edge.flag, edge.country, edge.port_raw, edge.arrived_on) == \
        ("Marshall Islands", "Indonesia", "INDONASIA", date(2026, 10, 19))
    exp, = _rows(st, "anchorage", "KANG HUA WAN")
    assert exp.direction == "export" and exp.country is None  # loading here ('PQA')
    sailed = [r for r in st.rows if r.section == "sailed"]
    assert len(sailed) == 5 and sailed[0].departs_at == datetime(2026, 10, 30, 6, 45)
    assert not [r for r in st.rows if r.section == "expected"]  # "No Ships are expected"
    assert st.berthed["Total"] == 37939 and sum(v for k, v in st.berthed.items() if k != "Total") == 37939


def test_oldest_layout_two_cargoes_containers_without_manifest():
    st = _parse("pqa_statement_2019-07-16.pdf")
    assert st.day == date(2019, 7, 16)
    tanker = {r.commodity_raw: r for r in _rows(st, "berth", "SC HONG KONG")}
    assert set(tanker) == {"PARAXYLENE", "M.E.G"}
    assert all(r.direction == "import" for r in tanker.values())
    assert (tanker["M.E.G"].manifest, tanker["M.E.G"].handled_24h, tanker["M.E.G"].handled_before) == (3469, 169, 3300)
    cement, = _rows(st, "berth", "LORENTZOS")
    assert (cement.direction, cement.manifest, cement.handled_24h, cement.balance) == ("export", 40731, 3222, 37509)
    boxes = {r.direction: r for r in _rows(st, "berth", "CPO HAMBURG")}
    assert boxes["import"].manifest is None and boxes["import"].handled_24h == 611
    assert boxes["export"].handled_24h == 649
    # an L.O.A-first anchorage table; a ship with two cargoes; LNG in cubic metres
    torm = {r.commodity: r for r in _rows(st, "anchorage", "TORM VENCORE")}
    assert torm["Diesel (gas oil)"].manifest == 5988 and torm["Petrol"].manifest == 39983  # GAS OIL, GASOLLENE
    ejnan, = _rows(st, "anchorage", "EJNAN")
    assert (ejnan.commodity, ejnan.unit, ejnan.manifest, ejnan.country) == ("LNG", "cubic metre", 141150, "Qatar")
    assert [r.vessel for r in st.rows if r.section == "expected"] == ["MAERSK SELETAR"]


def test_empty_anchorage_reads_zero_not_missing():
    st = _parse("pqa_statement_2026-03-21.pdf")  # the anchorage heading, then no ships
    assert st.day == date(2026, 3, 21)
    assert not [r for r in st.rows if r.section == "anchorage"] and "anchorage_heading" in st.layout
    assert [r for r in st.rows if r.section == "berth"]
    recs = {(r.series_id, tuple(sorted(r.dims.items()))): r.value for r in to_records(st)}
    assert recs[("ports.qasim.ships_at_anchorage", (("direction", "all"), ("level", "total")))] == 0
    assert recs[("ports.qasim.cargo_at_anchorage", (("direction", "all"), ("level", "total")))] == 0
    # without the heading (a section that wasn't read) there is no figure at all
    st.layout.discard("anchorage_heading")
    assert not [r for r in to_records(st) if r.series_id == "ports.qasim.ships_at_anchorage"]


def test_records_one_unit_per_series_and_headline_dims():
    st = _parse("pqa_statement_2026-10-31.pdf")
    recs = {(r.series_id, tuple(sorted(r.dims.items()))): r.value for r in to_records(st)}
    tot = lambda d="all": (("direction", d), ("level", "total"))  # noqa: E731
    tonnes = [r for r in st.rows if r.section == "berth" and r.unit == "metric ton" and r.checks_ok is not False]
    assert recs[("ports.qasim.cargo_handled", tot())] == sum(r.handled_24h or 0 for r in tonnes)
    assert recs[("ports.qasim.cargo_handled", tot("export"))] == sum(
        r.handled_24h or 0 for r in tonnes if r.direction == "export")
    assert recs[("ports.qasim.containers_handled", tot("import"))] == 65 + 552
    assert recs[("ports.qasim.cargo_handled", (("commodity", "Coal"), ("direction", "import"),
                                               ("level", "commodity")))] == 23500
    assert recs[("ports.qasim.ships_at_anchorage", tot())] == 22
    assert recs[("ports.qasim.berthings_to_date", (("level", "total"),))] == 37939
    assert ("ports.qasim.lng_handled", (("level", "total"),)) in recs  # a day without LNG reads 0
    # each point once: (series, dims) keys are unique by construction, and every value is a count or amount
    assert all(v >= 0 for v in recs.values())


def test_out_of_range_and_redated_statements_are_refused():
    assert checked_date(date(2002, 9, 26)) == date(2002, 9, 26)  # PQA's 2002 reprint, filed as 30-Jul-2019: kept
    with pytest.raises(ValueError, match="impossible"):
        checked_date(date(2031, 1, 1))
    assert checked_date(date(2023, 9, 20)) == date(2023, 9, 20)  # filed as 'mar19-20-SEP-2022': header wins
    job = PqaShippingJob()
    job._totals = {}
    with open(os.path.join(FIXTURES, "pqa_statement_2026-10-31.pdf"), "rb") as fh:
        content = fh.read()
    job.parse(FetchedFile("a.PDF", content, date(2026, 10, 31)))
    job.parse(FetchedFile("b.PDF", content, date(2026, 10, 31)))  # a re-upload: same running count
    job._totals[date(2026, 10, 31)] = 37900
    with pytest.raises(ValueError, match="stale date"):
        job.parse(FetchedFile("c.PDF", content, date(2026, 10, 31)))


def test_chain_confirms_container_lines_across_days():
    from ingestion.jobs.pqa_shipping import Statement, chain
    from ingestion.port_calls import VesselRow

    def row(day, prev, h, ok=None):
        return VesselRow(port="PQA", statement_date=day, section="berth", vessel="X", call_ref="1", direction="import",
                         commodity_raw="CONTAINERS", unit="teu", handled_before=prev, handled_24h=h, checks_ok=ok)

    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    a, b, c = row(d1, 0, 300), row(d2, 300, 200), row(d3, 600, 50)  # c restates: 300 + 200 = 500, not 600
    days: dict = {}
    for d, r in ((d2, b), (d1, a), (d3, c)):  # any order
        chain(Statement(day=d, rows=[r], berthed={}), days)
    assert (a.checks_ok, b.checks_ok, c.checks_ok) == (True, True, None)
    assert b.details["chain"] == "ok" and "implied 500" in c.details["chain"]


def test_helpers():
    assert statement_date("DAILY SHIPPING AND CARGO STATEMENT AS AT 0800 HRS 4 SG 9 AR ON 16 JUL 2019") == date(2019, 7, 16)
    assert figure("40,105") == (40105, None) and figure("NIL") == (0, None)
    assert figure("NO WORK") == (0, "no work") and figure("--") == (None, None)
    assert vessel_name("M.V- MAGIC CELESTE") == ("MAGIC CELESTE", "M.V")
    assert vessel_name("EVIRIDIKI M.T") == ("EVIRIDIKI", "M.T")
    subs = sub_rows([(346.1, "SOYABEAN"), (354.2, "SEED"), (362.4, "M.Ton"), (374.2, "PHOSP. ACID"), (382.4, "M.Ton")])
    assert [(s.text, s.unit) for s in subs] == [("SOYABEAN SEED", "M.Ton"), ("PHOSP. ACID", "M.Ton")]
    got = assign([SubRow(top=170.0, name=["RICE"]), SubRow(top=190.6, name=["CEMENT"])],
                 [(178.8, {"manifest": "8035"}), (199.5, {"manifest": "31549"})], anchor=171.7)
    assert [(s.text, d) for s, d, _ in got] == [("RICE", "export"), ("CEMENT", "export")]
    page = ('<a href="https://www.pqa.gov.pk/Content/Media//daily-shipping-and-cargo-handling-reports/'
            'dailyShippingCargoStat_31-OCT-2026-bc5b.PDF">x</a> <a href="?page=247">247</a>')
    assert album_links(page) == ["https://www.pqa.gov.pk/Content/Media//daily-shipping-and-cargo-handling-reports/"
                                 "dailyShippingCargoStat_31-OCT-2026-bc5b.PDF"]
    assert last_page(page) == 247


def test_recovered_statements_reference():
    from ingestion.jobs.pqa_shipping import recovered

    items = recovered()
    assert items and len({i["date"] for i in items}) == len(items)  # one statement per recovered day
    assert all(i["url"].startswith("https://www.pqa.gov.pk/Content/Media/") and i["wayback"].isdigit() for i in items)
