from __future__ import annotations

import json
import os
from datetime import date

import pytest

from ingestion import water_pk
from ingestion.framework import FetchedFile, dedupe_last
from ingestion.jobs import wapda_river_flows as w
from scripts import vintages_core as core
from scripts.vintages_wapda import vintage_issues

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _file(name: str, as_name: str | None = None) -> FetchedFile:
    with open(os.path.join(FIX, name), "rb") as f:
        return FetchedFile(as_name or name, f.read(), date(2026, 10, 31))


@pytest.fixture(scope="module")
def workbook():
    published, prints = w.file_prints(_file("wapda_river_flow_2024-12-20.xls", "GRAPH-DG-16-for-MAIL-2.xls"))
    assert published == date(2024, 12, 20)
    return {(p.series_id, p.dims, p.obs_date): p for p in prints}


def _v(prints, sid, when, **dims):
    p = prints[(sid, tuple(sorted(dims.items())), when)]
    return round(p.value * p.scale, 4)


def test_graph_sheet_first_day_every_column(workbook):
    d = date(2016, 3, 1)
    assert _v(workbook, w.LEVEL, d, site="Tarbela", river="Indus") == 1464.48
    assert _v(workbook, w.FLOW, d, site="Tarbela", river="Indus", measure="inflow") == 17000
    assert _v(workbook, w.FLOW, d, site="Tarbela", river="Indus", measure="outflow") == 63000
    assert _v(workbook, w.FLOW, d, site="Nowshera", river="Kabul", measure="inflow") == 9800
    assert _v(workbook, w.LEVEL, d, site="Mangla", river="Jhelum") == 1122.6
    assert _v(workbook, w.FLOW, d, site="Mangla", river="Jhelum", measure="inflow") == 16800
    assert _v(workbook, w.FLOW, d, site="Mangla", river="Jhelum", measure="outflow") == 60000
    assert _v(workbook, w.FLOW, d, site="Marala", river="Chenab", measure="inflow") == 7200
    # the four rim-station inflows summed, as WAPDA prints them
    assert _v(workbook, w.SYSTEM, d, measure="current") == 50800
    assert _v(workbook, w.SYSTEM, d, measure="last_year") == 72137
    assert _v(workbook, w.SYSTEM, d, measure="ten_year_average") == 81229.8


def test_graph_sheet_ends_at_the_last_reading(workbook):
    tarbela = [k[2] for k in workbook if k[0] == w.LEVEL and ("site", "Tarbela") in k[1]]
    assert min(tarbela) == date(2016, 3, 1) and max(tarbela) == date(2024, 12, 19)
    assert _v(workbook, w.LEVEL, date(2024, 12, 19), site="Tarbela", river="Indus") == 1472.95
    # rows after the last reading carry only the comparison columns
    assert (w.SYSTEM, (("measure", "current"),), date(2025, 1, 1)) not in workbook
    assert (w.SYSTEM, (("measure", "ten_year_average"),), date(2025, 3, 31)) in workbook
    # "last year" on 29 February: no such day a year earlier
    assert (w.SYSTEM, (("measure", "last_year"),), date(2020, 2, 29)) not in workbook
    assert (w.SYSTEM, (("measure", "current"),), date(2020, 2, 29)) in workbook


def test_kotri_sheet_seasons_side_by_side(workbook):
    kotri = dict(site="Kotri", river="Indus", measure="outflow")
    assert _v(workbook, w.FLOW, date(2023, 10, 1), **kotri) == 5470       # Rabi 2023-24, October
    assert _v(workbook, w.FLOW, date(2024, 9, 1), **kotri) == 335618      # Kharif 2024, September
    assert _v(workbook, w.FLOW, date(2024, 12, 19), **kotri) == 660       # Rabi 2024-25, December
    assert (w.FLOW, tuple(sorted(kotri.items())), date(2024, 12, 20)) not in workbook


def test_temp_sheet_days_from_day_numbers(workbook):
    def t(when, station, measure):
        return _v(workbook, w.TEMP, when, station=station, measure=measure)

    assert t(date(2017, 4, 1), "Skardu", "max") == 16.7
    assert t(date(2017, 4, 1), "Skardu", "min") == 7.8
    # the date cell reads 2017-04-11 but the row is the 21st
    assert t(date(2017, 4, 21), "Skardu", "max") == 24.4
    # 9 Oct 2021 is skipped in the sheet
    assert (w.TEMP, (("measure", "max"), ("station", "Skardu")), date(2021, 10, 9)) not in workbook
    assert (w.TEMP, (("measure", "max"), ("station", "Skardu")), date(2021, 10, 10)) in workbook
    # last day, matching WAPDA's daily press release of 19 Dec 2024 (Skardu 4.4 / -7.2)
    assert t(date(2024, 12, 19), "Skardu", "max") == 4.4
    assert t(date(2024, 12, 19), "Skardu", "min") == -7.2
    assert {dict(k[1])["station"] for k in workbook if k[0] == w.TEMP} == set(water_pk.CATCHMENT_STATIONS)


def test_web_calendar_year_table():
    published, prints = w.file_prints(_file("wapda_wayback_flows_20170501214120.html",
                                            "wayback_flows_20170501214120.html"))
    assert published == date(2017, 5, 1)
    days = sorted({p.obs_date for p in prints})
    assert days[0] == date(2015, 1, 1) and days[-1] == date(2015, 12, 31) and len(days) == 365
    got = {(p.series_id, p.dims, p.obs_date): p for p in prints}
    assert _v(got, w.LEVEL, date(2015, 12, 31), site="Tarbela", river="Indus") == 1489.62
    assert _v(got, w.FLOW, date(2015, 12, 31), site="Tarbela", river="Indus", measure="inflow") == 20900
    assert _v(got, w.SYSTEM, date(2015, 12, 31), measure="ten_year_average") == 43900
    assert got[(w.LEVEL, (("river", "Indus"), ("site", "Tarbela")), date(2015, 12, 31))].dp == 2


def test_web_rolling_table_years_from_capture_date():
    _, prints = w.file_prints(_file("wapda_wayback_flows_20190208191135.html", "wayback_flows_20190208191135.html"))
    days = sorted({p.obs_date for p in prints})
    # "6-Feb" at the top of a page captured on 8 Feb 2019, back to "1-Jan" two years earlier
    assert days[-1] == date(2019, 2, 6) and days[0] == date(2017, 1, 1)
    got = {(p.series_id, p.dims, p.obs_date): p for p in prints}
    assert _v(got, w.LEVEL, date(2019, 2, 6), site="Tarbela", river="Indus") == 1431.98
    assert _v(got, w.FLOW, date(2019, 2, 6), site="Marala", river="Chenab", measure="inflow") == 11500


def test_web_table_repeated_row_is_skipped():
    _, prints = w.file_prints(_file("wapda_wayback_flows_20170808183035.html", "wayback_flows_20170808183035.html"))
    days = sorted({p.obs_date for p in prints})
    assert days[0] == date(2017, 1, 1) and days[-1] == date(2017, 7, 31)


def _table(*days: str, year: str = "") -> str:
    head = ("<tr><td>RIVER FLOWS AND LEVELS " + year + "</td></tr><tr><td>PERIOD</td><td>INDUS AT TARBELA</td>"
            "<td>KABUL INFLOW AT NOWSHERA</td><td>JEHLUM AT MANGLA</td><td>CHENAB INFLOW AT MARALA</td>"
            "<td>SYSTEM INFLOWS</td></tr>")
    rows = "".join(f"<tr><td>{d}</td>" + "<td>1.0</td>" * 11 + "</tr>" for d in days)
    return "<table>" + head + rows + "</table>"


def test_web_table_out_of_order_rows_fail():
    assert len(w.web_flows(_table("2-Jan", "1-Jan", "31-Dec"), date(2019, 1, 3))) == 33
    with pytest.raises(ValueError):
        w.web_flows(_table("2-Jan", "1-Jan", "5-Jan", year="(YEAR 2019)"), date(2019, 3, 1))
    with pytest.raises(ValueError):  # a layout change: headings missing or reordered
        w.web_flows(_table("2-Jan").replace("MANGLA", "XX"), date(2019, 1, 3))


def test_web_kotri_grid():
    _, prints = w.file_prints(_file("wapda_wayback_kotri_20200928031552.html", "wayback_kotri_20200928031552.html"))
    got = {p.obs_date: p.value for p in prints}
    assert got[date(2019, 10, 1)] == 3963      # Rabi 2019-20, October
    assert got[date(2020, 3, 1)] == 0          # Rabi 2019-20, March
    assert got[date(2020, 4, 1)] == 11214      # Kharif 2020, April
    assert got[date(2020, 9, 1)] == 147772     # Kharif 2020, September
    assert all(p.series_id == w.FLOW and dict(p.dims)["site"] == "Kotri" for p in prints)


def test_season_dates():
    assert w.season_dates("RABI  2023-24            (Cusecs)", "JAN") == (2024, 1)
    assert w.season_dates("RABI 2023-24", "OCT") == (2023, 10)
    assert w.season_dates("KHARIF 2024 (Cusecs)", "SEP") == (2024, 9)
    assert w.season_dates("Date", "SEP") is None


def test_records_fit_the_catalog(workbook):
    recs = dedupe_last(w.to_records(list(workbook.values())))
    cat = {c["id"]: c for c in w.CATALOG}
    for r in recs:
        c = cat[r.series_id]
        assert c["min_value"] <= r.value <= c["max_value"], r
        for k, allowed in c["dimensions"].items():
            assert r.dims[k] in allowed, r
    sites = {r.dims["site"] for r in recs if "site" in r.dims}
    assert all(water_pk.canon_site(s).name == s for s in sites)


def test_vintages_from_successive_copies():
    old = w.file_prints(_file("wapda_wayback_flows_20190208191135.html", "wayback_flows_20190208191135.html"))
    new = w.file_prints(_file("wapda_river_flow_2024-12-20.xls", "GRAPH-DG-16-for-MAIL-2.xls"))
    issues = vintage_issues([("workbook", *new), ("wayback_flows_20190208191135", *old)])
    assert [i["published"] for i in issues] == ["2019-02-08", "2024-12-20"]
    assert all("series_id" in r for i in issues for r in i["rows"])
    # the workbook issue only carries what the 2019 page didn't already print the same way
    n_old = sum(len(r["values"]) for r in issues[0]["rows"])
    n_new = sum(len(r["values"]) for r in issues[1]["rows"])
    assert n_new < len(new[1]) and n_old == len(old[1])
    current = {(p.series_id, core.dims_key(dict(p.dims)), p.obs_date.isoformat()): round(p.value * p.scale, 4)
               for p in new[1]}
    issues = json.loads(json.dumps(issues))
    first_seen, revisions = core.build_pinned(issues, current, date(2026, 8, 17))
    kabul = (w.FLOW, core.dims_key({"site": "Nowshera", "river": "Kabul", "measure": "inflow"}), "2017-10-10")
    assert first_seen[kabul] == "2019-02-08"
    # printed 6.2 (thousand cusecs) in 2019; the 2024 workbook reads 0.0
    assert (*kabul, 6200.0, "2024-12-20", "wapda_web") in revisions
    assert current[kabul] == 0.0
    # days the 2019 page didn't cover are first seen in the workbook
    late = (w.LEVEL, core.dims_key({"site": "Tarbela", "river": "Indus"}), "2024-12-19")
    assert first_seen[late] == "2024-12-20"


def test_rebuild_keeps_earlier_copies_and_adds_a_changed_workbook():
    old = w.file_prints(_file("wapda_wayback_kotri_20200928031552.html", "wayback_kotri_20200928031552.html"))
    first = vintage_issues([("wayback_kotri_20200928031552", *old)])
    # the same capture again is skipped; a new copy printing a different figure is added on top
    changed = [w.Print(p.series_id, p.dims, p.obs_date, p.value + (100 if p.obs_date == date(2019, 10, 1) else 0),
                       p.dp, p.scale) for p in old[1]]
    again = vintage_issues([("wayback_kotri_20200928031552", *old), ("workbook-ffff", date(2027, 1, 5), changed)],
                           previous=first)
    assert [i["issue"] for i in again] == ["wayback_kotri_20200928031552", "workbook-ffff"]
    assert again[1]["rows"][0]["values"] == {"2019-10-01": [4063.0, 0]}


def test_workbook_fixture_is_the_2024_upload():
    with open(os.path.join(FIX, "wapda_river_flow_2024-12-20.xls"), "rb") as fh:
        assert w.workbook_sha(fh.read()) == w.WORKBOOK_SHA


def test_same_print_tolerates_precision_not_revisions():
    assert core.same_print((None, 147450.0, 2, 1000.0), (None, 147500.0, 1, 1000.0))
    assert not core.same_print((None, 17000.0, 1, 1000.0), (None, 17300.0, 1, 1000.0))


def test_split_pinned_keeps_value_matched_rows_apart():
    issues = [{"issue": "a", "published": "2020-01-01", "source": "s", "rows": [
        {"label": "x", "values": {}}, {"label": "y", "series_id": "s.y", "values": {}}]}]
    plain, pinned = core.split_pinned(issues)
    assert [r["label"] for r in plain[0]["rows"]] == ["x"]
    assert [r["label"] for r in pinned[0]["rows"]] == ["y"]


def test_wayback_list_is_oldest_first_and_unique():
    stamps = [ts for _k, ts, _u in w.WAYBACK]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert {k for k, _t, _u in w.WAYBACK} == {"flows", "kotri"}
