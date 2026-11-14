"""PCGA cotton arrivals parser (ingestion/jobs/pcga_cotton_arrivals.py). Offline; needs pdfplumber.
Fixtures are real PCGA reports: the current layout (18-Oct-2026), the end-of-season layout without
last season's comparison and a stacked "DATA WAS NOT COLLECTED" note (18-May-2019), a report with
a blank cell (Bhakkar's factories, 03-Sep-2024), a 2016-17 report whose "last year" figures are the only
record of the 2015-16 season (03-Feb-2017), and the 18-Oct-2026 report as a transcription table (the
format a scanned report is read into)."""
from __future__ import annotations

import os
from datetime import date

import pytest

pytest.importorskip("pdfplumber")

from ingestion.jobs.pcga_cotton_arrivals import (
    failed_columns, fills_last_year, parse_last_year, parse_lines, parse_text, pdf_lines, place, published,
    report_links, report_records, split_row, to_records, transcribed_lines,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _parse(name: str):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return parse_lines(pdf_lines(fh.read()))


def _row(rows, label):
    return next(r for r in rows if r["label"].startswith(label))


def test_current_layout_reads_every_district_and_adds_up():
    when, rows, totals = _parse("pcga_arrivals_2026-10-18.pdf")
    assert when == date(2026, 10, 15)                       # "AS ON: 15 Oct 2026."
    assert len(rows) == 33                                   # 21 Punjab + 11 Sindh + Balochistan
    assert failed_columns(rows, totals) == {}
    multan = _row(rows, "MULTAN")
    assert (multan["arrivals"], multan["pressed"], multan["sold"], multan["unsold_stock"],
            multan["unginned_stock"], multan["fortnightly_arrivals"], multan["factories_working"]) == \
        (23977, 23277, 20409, 2868, 700, 3852, 5)
    # the label overflowed into its first number: "(THARPARKA5R5),050"
    mpk = _row(rows, "MIRPUR KHAS")
    assert (mpk["district"], mpk["arrivals"], mpk["pressed"]) == ("Mirpur Khas & Tharparkar", 55050, 52700)
    assert _row(rows, "QASUR")["district"] == "Kasur" and _row(rows, "BADEEN")["district"] == "Badin"
    assert _row(rows, "NAWABSHAH")["district"] == "Shaheed Benazirabad"
    assert totals["pakistan"]["arrivals"] == 4081265


def test_records_cover_districts_provinces_and_pakistan():
    when, rows, totals = _parse("pcga_arrivals_2026-10-18.pdf")
    recs = {(r.series_id, tuple(sorted(r.dims.items()))): r.value for r in to_records(when, rows, totals, set())}
    arr = "agriculture.cotton.arrivals"
    nat, prov = (("level", "national"),), lambda p: (("level", "province"), ("province", p))
    assert recs[(arr, nat)] == 4081265
    assert recs[(arr, prov("Punjab"))] == 1638497
    # PCGA's Sindh total includes Balochistan's row; Sindh is its own districts
    assert recs[(arr, prov("Balochistan"))] == 180607
    assert recs[(arr, prov("Sindh"))] == 2442768 - 180607
    assert recs[(arr, (("district", "Sanghar"), ("level", "district"), ("province", "Sindh")))] == 1137903
    sales = "agriculture.cotton.sales"
    assert recs[(sales, (("buyer", "all"),) + nat)] == 3549735
    assert recs[(sales, (("buyer", "exporters"),) + nat)] == 97400
    assert recs[(sales, (("buyer", "textile_mills"),) + nat)] == 3452335
    assert recs[("agriculture.cotton.factories_working", nat)] == 471
    assert {r.obs_date for r in to_records(when, rows, totals, set())} == {date(2026, 10, 15)}


def test_end_of_season_layout_picks_up_cells_pushed_below_a_stacked_note():
    when, rows, totals = _parse("pcga_arrivals_2019-05-18.pdf")
    assert when == date(2019, 5, 15)                         # "AS ON 15TH MAY 2019."
    lodhran = _row(rows, "LODHRAN")                          # its last two cells sit on a line of their own
    assert (lodhran["arrivals"], lodhran["fortnightly_arrivals"], lodhran["factories_working"]) == (146610, 0, 0)
    assert len(rows) == 33 and failed_columns(rows, totals) == {}
    assert totals["pakistan"]["arrivals"] == 10777818


def test_blank_cell_stays_blank_instead_of_shifting_the_row():
    when, rows, totals = _parse("pcga_arrivals_2024-09-03.pdf")
    assert when == date(2024, 8, 31)
    bhakkar = _row(rows, "BHAKKAR")                          # factories cell is blank in the report
    assert (bhakkar["fortnightly_arrivals"], bhakkar["factories_working"]) == (310, None)
    assert failed_columns(rows, totals) == {}
    recs = to_records(when, rows, totals, set())
    assert not any(r.dims.get("district") == "Bhakkar" and r.series_id.endswith("factories_working") for r in recs)


def test_a_column_that_does_not_add_up_is_left_out():
    when, rows, totals = _parse("pcga_arrivals_2026-10-18.pdf")
    _row(rows, "MULTAN")["unsold_stock"] += 1000             # breaks pressed − sold = unsold
    _row(rows, "VEHARI")["arrivals"] += 5                    # breaks the Punjab sum
    bad = failed_columns(rows, totals)
    assert set(bad) == {"unsold_stock", "arrivals", "unginned_stock"}
    recs = to_records(when, rows, totals, set(bad))
    assert not any(r.series_id in ("agriculture.cotton.arrivals", "agriculture.cotton.unsold_stock") for r in recs)
    assert any(r.series_id == "agriculture.cotton.pressed" for r in recs)


def test_split_row_and_places():
    label, nums = split_row([("MIRPUR", 1.0), ("KHAS", 2.0), ("(THARPARK1A2R1,)942", 242.0), ("121,345", 301.0),
                             ("(15,050)", 700.0), ("(SHORT)", 750.0), ("(21.47%)", 800.0), ("-", 820.0)])
    assert label == "MIRPUR KHAS (THARPARKAR)"
    assert nums == [(121942.0, 242.0), (121345.0, 301.0), (-15050.0, 700.0)]
    assert place("BALUCHISTAN") == ("Balochistan", None)
    assert place("T. T. SINGH") == ("Punjab", "Toba Tek Singh")
    assert place("MUZAFFAR GARH") == ("Punjab", "Muzaffargarh")
    assert place("MIRPUR KHAS (THARP") == ("Sindh", "Mirpur Khas & Tharparkar")


def test_plain_text_reads_counts_in_order():
    text = ("CONSOLIDATED STATEMENT OF COTTON ARRIVALS IN FACTORIES OF PAKISTAN AS ON: 01ST. October 2016.\n"
            "1 MULTAN 15,983 8,203 0 0 5,900 5,900 2,303 7,780 13,155 2,828 INCREASE 21.50% 15,983 15\n"
            "TOTAL OF PUNJAB: 15,983 8,203 0 0 5,900 5,900 2,303 7,780 13,155 2,828 INCREASE 21.50% 15,983 15\n"
            "1 HYDERABAD 100 100 0 0 100 100 0 0 90 10 INCREASE 11.1% 100 1\n"
            "TOTAL OF SINDH 100 100 0 0 100 100 0 0 90 10 INCREASE 11.1% 100 1\n"
            "TOTAL OF PAKISTAN: 16,083 8,303 0 0 6,000 6,000 2,303 7,780 13,245 2,838 INCREASE 1% 16,083 16\n")
    when, rows, totals = parse_text(text)
    assert when == date(2016, 10, 1)
    assert [r["district"] for r in rows] == ["Multan", "Hyderabad"]
    assert rows[0]["fortnightly_arrivals"] == 15983 and rows[0]["factories_working"] == 15
    assert failed_columns(rows, totals) == {}


def test_report_links_sort_by_publication_date():
    base = "https://www.pcga.org/wp-content/uploads"
    page = (f'<a href="{base}/2021/09/03-September-2021.pdf">x</a>'            # repeated old link at the top
            f'<a href="{base}/2026/10/18-October-2026.pdf">18 October 2026</a>'
            f'<a href="{base}/2026/10/03-October-2026.pdf">03 October 2026</a>'
            f'<a href="{base}/2026/10/18-September-2026.pdf">18 September 2026</a>'
            f'<a href="{base}/2020/03/10-01-February-2019.pdf">01 February 2019</a>'
            f'<a href="{base}/2020/03/membership-list-2019-20-of-north-zone.pdf">members</a>'
            f'<a href="{base}/2021/09/03-September-2021.pdf">03 September 2021</a>')
    links = [u.rsplit("/", 1)[-1] for u in report_links(page)]
    assert links == ["10-01-February-2019.pdf", "03-September-2021.pdf", "18-September-2026.pdf",
                     "03-October-2026.pdf", "18-October-2026.pdf"]
    assert published(f"{base}/2020/11/01-May-2018.Final_.pdf", ("2020", "11")) == date(2018, 5, 1)
    assert published(f"{base}/2022/01/3-JANUARY-2022.pdf", ("2022", "01")) == date(2022, 1, 3)


def _key(recs):
    return {(r.series_id, r.obs_date, tuple(sorted(r.dims.items()))): r.value for r in recs}


def test_last_season_fills_a_date_before_the_archive():
    with open(os.path.join(FIXTURES, "pcga_arrivals_2017-02-03.pdf"), "rb") as fh:
        lines = pdf_lines(fh.read())
    ly_when, ly_totals = parse_last_year(lines)                   # header "ARRIVALS AS ON:01.02.2016"
    assert ly_when == date(2016, 2, 1) and fills_last_year(ly_when)
    assert set(ly_totals) == {"punjab", "sindh", "pakistan"}
    recs = _key(report_records(lines))
    feb16 = date(2016, 2, 1)
    arr, nat = "agriculture.cotton.arrivals", (("level", "national"),)
    assert recs[(arr, date(2017, 2, 1), nat)] == 10634627         # the report's own date is untouched
    assert recs[(arr, feb16, nat)] == 9612711                       # LAST YEAR'S TOTAL under Pakistan
    assert recs[("agriculture.cotton.pressed", feb16, (("level", "province"), ("province", "Punjab")))] == 5803961
    assert recs[(arr, feb16, (("district", "Multan"), ("level", "district"), ("province", "Punjab")))] == 128079
    # Sindh = its districts; PCGA's Sindh total also holds Balochistan, so Sindh's other columns aren't split
    assert recs[(arr, feb16, (("level", "province"), ("province", "Sindh")))] == 3755015 - 57045
    assert recs[(arr, feb16, (("level", "province"), ("province", "Balochistan")))] == 57045
    assert not any(k[1] == feb16 and k[0] != arr and ("province", "Sindh") in k[2] for k in recs)


def test_last_season_is_not_stored_where_that_date_has_its_own_report():
    with open(os.path.join(FIXTURES, "pcga_arrivals_2026-10-18.pdf"), "rb") as fh:
        lines = pdf_lines(fh.read())
    assert parse_last_year(lines)[0] == date(2025, 10, 15) and not fills_last_year(date(2025, 10, 15))
    assert {r.obs_date for r in report_records(lines)} == {date(2026, 10, 15)}


def test_a_transcribed_scan_reads_like_the_text_report():
    with open(os.path.join(FIXTURES, "pcga_arrivals_2026-10-18.pdf"), "rb") as fh:
        from_pdf = _key(report_records(pdf_lines(fh.read())))
    with open(os.path.join(FIXTURES, "pcga_transcribed_2026-10-18.md"), encoding="utf-8") as fh:
        md = fh.read()
    assert _key(report_records(transcribed_lines(md))) == from_pdf
    # an empty cell stays blank instead of shifting the row
    row = next(ln for ln in md.splitlines() if "| MULTAN |" in ln)
    cells = row.split("|")
    assert cells[6].strip() == "0"                                # Multan's exporters cell
    cells[6] = "  "
    when, rows, totals = parse_lines(transcribed_lines(md.replace(row, "|".join(cells))))
    multan = _row(rows, "MULTAN")
    assert (multan["sold_exporters"], multan["sold"], multan["factories_working"]) == (None, 20409, 5)
    assert failed_columns(rows, totals) == {}
