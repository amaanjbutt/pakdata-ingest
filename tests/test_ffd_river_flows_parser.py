from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion import water_pk
from ingestion.jobs import ffd_river_flows as f

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _lines(name: str) -> list[str]:
    with open(os.path.join(FIX, name), "rb") as fh:
        content = fh.read()
    return f.pdf_lines(content) if content[:4] == b"%PDF" else f.html_lines(content.decode("utf-8"))


def test_2024_pdf_with_design_capacity_column():
    b = f.parse_lines(_lines("ffd_bulletin_2024-08-15.pdf"))
    assert b["date"] == date(2024, 8, 15)
    assert len(b["flows"]) == 22
    assert b["flows"]["Tarbela"] == (285.0, 255.3)
    assert b["flows"]["Guddu"] == (388.0, 357.4)
    assert b["flows"]["Nowshera"] == (55.8, 55.8)          # capacity printed as "-"
    assert b["flows"]["Ganda Singh Wala"] == (3.6, 3.6)    # "G.S. Wala --"
    assert b["flows"]["Panjnad"] == (48.6, 35.8)           # printed "Punjnad"
    assert b["levels"] == {"Tarbela": 1548.0, "Mangla": 1211.25}
    assert b["storage"] == {"Tarbela": 98.0, "Mangla": 68.5}


def test_2025_pdf_adds_chiniot_bridge():
    b = f.parse_lines(_lines("ffd_bulletin_2025-10-15.pdf"))
    assert b["date"] == date(2025, 10, 15)
    assert len(b["flows"]) == 23
    assert b["flows"]["Chiniot Bridge"] == (2.6, 2.6)
    assert b["flows"]["Kotri"] == (59.9, 36.4)
    assert b["flows"]["Islam"] == (13.5, 11.2)
    assert b["levels"] == {"Tarbela": 1550.0, "Mangla": 1242.0}
    assert b["storage"] == {"Tarbela": 100.0, "Mangla": 100.0}


def test_2026_html_old_layout():
    b = f.parse_lines(_lines("ffd_bulletin_2026-06-17.html"))
    assert b["date"] == date(2026, 6, 17)
    assert b["flows"]["Tarbela"] == (139.0, 145.5)
    assert b["flows"]["Nowshera"] == (50.2, 50.2)
    assert b["flows"]["Mangla"] == (34.0, 34.5)
    assert b["levels"] == {"Tarbela": 1445.15, "Mangla": 1172.1}
    assert b["storage"] == {"Tarbela": 15.19, "Mangla": 36.83}


def test_2026_html_without_capacity_column_and_limit_chart_ignored():
    b = f.parse_lines(_lines("ffd_bulletin_2026-10-14.html"))
    assert b["date"] == date(2026, 10, 14)
    assert len(b["flows"]) == 22
    assert b["flows"]["Tarbela"] == (45.0, 44.3)
    assert b["flows"]["Mangla"] == (6.0, 26.4)
    assert b["flows"]["Kotri"] == (17.9, 0.3)
    assert b["flows"]["Sidhnai"] == (13.0, 0.0)
    assert b["levels"] == {"Tarbela": 1518.88, "Mangla": 1199.45}
    assert b["storage"] == {"Tarbela": 69.52, "Mangla": 75.25}


def test_bulletin_without_gauge_table():
    assert f.parse_lines(_lines("ffd_bulletin_2024-06-15.pdf")) is None


def test_records_in_cusecs_with_site_river_measure():
    recs = f.to_records(f.parse_lines(_lines("ffd_bulletin_2024-08-15.pdf")))
    got = {(r.series_id, tuple(sorted(r.dims.items()))): r.value for r in recs}
    assert got[("water.ffd.river_flow", (("measure", "inflow"), ("river", "Indus"), ("site", "Tarbela")))] == 285000
    assert got[("water.ffd.river_flow", (("measure", "outflow"), ("river", "Chenab"), ("site", "Marala")))] == 64100
    assert got[("water.ffd.reservoir_level", (("river", "Jhelum"), ("site", "Mangla")))] == 1211.25
    assert got[("water.ffd.reservoir_storage", (("river", "Indus"), ("site", "Tarbela")))] == 98.0
    assert all(r.obs_date == date(2024, 8, 15) for r in recs)
    cat = {c["id"]: c for c in f.CATALOG}
    for r in recs:
        assert cat[r.series_id]["min_value"] <= r.value <= cat[r.series_id]["max_value"]
        assert r.dims["site"] in cat[r.series_id]["dimensions"]["site"]
        assert r.dims["river"] == water_pk.canon_site(r.dims["site"]).river


def test_station_rows():
    assert f.station_row("SUTLEJ G. S. Wala -- 0.0 0.0 No sig. change")[0].name == "Ganda Singh Wala"
    assert f.station_row("Chiniot Bridge 807 2.6 2.6 -do-")[0].name == "Chiniot Bridge"
    assert f.station_row("INDUS | Tarbela | 44.0 | 55.2 | 40 - 45")[1][:2] == ["44.0", "55.2"]
    assert f.station_row("Tarbela Below Low Low Medium High VH") is None
    assert f.station_row("Chief Engineer, Mangla/Tarbela/Chashma") is None


def test_station_without_printed_flows_is_dropped_not_the_bulletin():
    # 7 Oct 2025: Mangla's cells are blank and the row reads "JHELUM Mangla 1060 15 -30 Below low"
    b = f.parse_lines(_lines("ffd_bulletin_2025-10-07.pdf"))
    assert b["date"] == date(2025, 10, 7)
    assert b["dropped"] == ["Mangla"] and "Mangla" not in b["flows"]
    assert b["flows"]["Tarbela"] == (60.0, 62.6) and b["flows"]["Rasul"] == (4.7, 0.0)
    lines = _lines("ffd_bulletin_2026-10-14.html")
    bad = f.parse_lines([ln.replace("Tarbela 45.0 44.3", "Tarbela 4500.0 44.3") for ln in lines])
    assert "Tarbela" in bad["dropped"] and bad["flows"]["Kotri"] == (17.9, 0.3)
    with pytest.raises(ValueError):  # most stations unreadable: the layout changed
        f.parse_lines([ln.replace(".", ",") if " 0600 " not in ln else ln for ln in lines])


def test_listing_pages():
    with open(os.path.join(FIX, "ffd_bulletin_list_2026.html"), encoding="utf-8") as fh:
        ids = f.current_ids(fh.read())
    assert ids[-1] == 136 and 125 in ids
    with open(os.path.join(FIX, "ffd_bulletin_archive_p1.html"), encoding="utf-8") as fh:
        arch = f.archive_ids(fh.read())
    assert len(arch) == 30 and 191 in arch


def test_bulletin_date_formats():
    assert f.bulletin_date("BULLETIN No. B-062/24 Dated: - 15th August, 2024") == date(2024, 8, 15)
    assert f.bulletin_date("Dated: 15th October-2025") == date(2025, 10, 15)
    assert f.bulletin_date("Dated: 2nd August-2026") == date(2026, 8, 2)
    with pytest.raises(ValueError):
        f.bulletin_date("no date here")


def test_daily_fetch_reads_the_newest_three_and_falls_back_to_the_archive(monkeypatch):
    with open(os.path.join(FIX, "ffd_bulletin_list_2026.html"), "rb") as fh:
        listing = fh.read()
    with open(os.path.join(FIX, "ffd_bulletin_archive_p1.html"), "rb") as fh:
        archive = fh.read()
    pages = {f.CURRENT_LIST.format(page=1): listing, f.ARCHIVE_LIST.format(page=1): archive}
    job = f.FfdRiverFlowsJob()
    monkeypatch.setattr(job, "http_get", lambda url: pages.get(url, b"doc"))
    assert [x.filename for x in job.fetch()] == ["ffd_bulletin_134.html", "ffd_bulletin_135.html",
                                                 "ffd_bulletin_136.html"]
    pages[f.CURRENT_LIST.format(page=1)] = b"<html>no bulletins this season yet</html>"
    assert [x.filename for x in job.fetch()] == ["ffd_archive_191.pdf", "ffd_archive_186.pdf",
                                                 "ffd_archive_182.pdf"]
    pages[f.ARCHIVE_LIST.format(page=1)] = b"<html></html>"
    with pytest.raises(ValueError):
        job.fetch()


def test_archive_pdf_dated_by_its_table_page():
    # the archive's PDF for 13 Oct 2024 binds that day's covering letter to the table of 3 Oct
    # (its own "BULLETIN No. B-111/24 Dated: 03rd October, 2024")
    b = f.parse_pdf(open(os.path.join(FIX, "ffd_bulletin_2024-10-13_with_2024-10-03_table.pdf"), "rb").read())
    assert b["date"] == date(2024, 10, 3)
    assert b["flows"]["Tarbela"] == (71.0, 79.3)
    assert b["levels"] == {"Tarbela": 1536.62, "Mangla": 1212.85}


def test_cells_wrapped_onto_the_next_line():
    b = f.parse_pdf(open(os.path.join(FIX, "ffd_bulletin_2024-09-05.pdf"), "rb").read())
    assert b["date"] == date(2024, 9, 5) and not b["dropped"]
    assert b["flows"]["Tarbela"] == (151.0, 155.3)       # "Tarbela 1500" / "151.0 155.3"
    assert b["flows"]["Kalabagh"] == (190.9, 185.2)


def test_overlapping_words_read_in_writing_order():
    # by position the table reads "CHENAMarala", "Qadiraba", "Is i lam": 13 stations
    content = open(os.path.join(FIX, "ffd_bulletin_2025-08-10.pdf"), "rb").read()
    assert len(f.parse_lines(f.pdf_lines(content))["flows"]) == 13
    b = f.parse_pdf(content)
    assert b["date"] == date(2025, 8, 10) and len(b["flows"]) == 18
    assert b["flows"]["Marala"] == (58.4, 27.6) and b["flows"]["Islam"] == (21.2, 19.3)
    assert b["flows"]["Tarbela"] == (211.0, 253.9)
