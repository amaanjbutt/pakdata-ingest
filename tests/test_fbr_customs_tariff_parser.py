"""fbr_customs_tariff: FBR's Pakistan Customs Tariff editions, parsed from trimmed pages of the real files."""
from __future__ import annotations

import os

from ingestion.jobs.fbr_customs_tariff import (
    EDITIONS,
    TariffLine,
    fta_rates,
    in_force_from,
    merge_edition,
    parse_file,
    parse_rate,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _parse(fn: str, fy: int) -> dict[str, TariffLine]:
    with open(os.path.join(FIX, fn), "rb") as f:
        return {r.code: r for r in parse_file(fn, f.read(), fy, "https://download1.fbr.gov.pk/Docs/" + fn)}


def test_fy2026_27_first_page_codes_descriptions_and_hierarchy():
    rows = _parse("fbr_tariff_2026-27_pages.pdf", 2027)
    r = rows["01012100"]
    assert r.fy == 2027 and r.cd == (0.0, None)
    assert r.description == "- - Pure-bred breeding animals"
    assert r.context == "Live horses, asses, mules and hinnies. > Horses"
    assert rows["01022110"].context == "Live bovine animals. > Cattle > Pure-bred breeding animals"
    assert rows["01031000"].cd == (20.0, None)
    assert rows["01051100"].cd == (5.0, None)
    # a wrapped description line joins its code's line
    assert rows["01061200"].description.startswith("- - Whales, dolphins and porpoises (mammals of the order Cetacea)")
    # headings are not codes
    assert not any(len(c) != 8 for c in rows)


def test_specific_duties_printed_as_rotated_glyphs():
    rows = _parse("fbr_tariff_2026-27_pages.pdf", 2027)
    assert rows["15071000"].cd == (None, "Rs.10550/MT")
    assert rows["15079000"].cd == (None, "Rs.11700/MT")
    assert rows["15119020"].cd == (None, "Rs.10800/MT")
    assert rows["15119020"].description == "- - - RBD Palm Oil"   # the rotated cell never leaks into the text


def test_wrapped_specific_duty_and_spaced_digits():
    rows = _parse("fbr_tariff_2026-27_pages.pdf", 2027)
    assert rows["37061000"].cd == (None, "Rs.5/meter")          # "Rs. 5 per" / "meter"
    assert rows["84213920"].cd == (10.0, None)                   # "1" "0" in a spaced font
    assert rows["84213910"].cd == (0.0, None)


def test_fy2009_10_keeps_the_years_rate_not_the_pre_budget_column():
    rows = _parse("fbr_tariff_2009-10_page3.pdf", 2010)
    assert rows["02071400"].cd == (25.0, None)
    assert rows["02071400"].description == "- - Cuts and offal, frozen"   # codes aligned with their own text
    assert rows["02072400"].context.endswith("> Of turkeys")
    assert rows["02081000"].cd == (20.0, None)


def test_fy2008_09_code_column_further_right():
    rows = _parse("fbr_tariff_2008-09_page5.pdf", 2009)
    assert rows["04031000"].cd == (35.0, None)
    assert rows["04041010"].cd == (25.0, None)
    assert rows["04061010"].context == "Cheese and curd. > Fresh (unripened or uncured) cheese, including whey cheese, and curd"
    assert rows["04090000"].description == "Natural honey."


def test_fy2013_14_workbook_prints_the_whole_duty_stack():
    rows = _parse("fbr_tariff_2013-14_trimmed.xlsx", 2014)
    r = rows["01012100"]
    assert r.cd == (5.0, None) and r.unit == "u"
    assert r.duties["st"] == (16.0, None) and r.duties["wht"] == (5.0, None)
    assert r.duties["fta:CN_FTA"] == (0.0, None)
    assert rows["07019000"].duties["rd_acd"] == (None, "RD 25")
    oil = rows["15071000"]
    assert oil.cd == (None, "Rs.9050/MT")
    assert oil.duties["fed"] == (None, "16% ad val and Rs.1/Kg") and oil.duties["st"] == (16.0, None)


def test_fy2013_14_budget_amendment_overlays_the_workbook():
    with open(os.path.join(FIX, "fbr_tariff_2013-14_budget_amendment_p1.pdf"), "rb") as f:
        amend = parse_file("a.pdf", f.read(), 2014, "amendment")
    rows = {r.code: r for r in amend}
    assert rows["14049020"].cd == (None, "Rs.300/Kg")
    assert rows["29414000"].cd == (10.0, None)                   # description words spilling into the column
    assert rows["29414000"].description == "- Chloramphenicol and its derivatives; salts thereof"
    base = TariffLine(2014, "29414000", "- Chloramphenicol", "Antibiotics.", {"cd": (5.0, None), "st": (16.0, None)}, "kg")
    merged = {r.code: r for r in merge_edition([[base], amend])}
    m = merged["29414000"]
    assert m.cd == (10.0, None) and m.duties["st"] == (16.0, None) and m.unit == "kg"
    assert m.context == "Antibiotics."                           # the workbook's hierarchy is kept


def test_merge_keeps_the_first_printing_with_a_rate():
    a = TariffLine(2010, "98011000", "Services", "", {})
    b = TariffLine(2010, "98011000", "Services", "", {"cd": (0.0, None)})
    assert merge_edition([[a, b]])[0].cd == (0.0, None)


def test_parse_rate():
    assert parse_rate("20") == (20.0, None)
    assert parse_rate("15%") == (15.0, None)
    assert parse_rate(5) == (5.0, None)
    assert parse_rate("MT Rs.10800/") == (None, "Rs.10800/MT")
    assert parse_rate("Rs.10800MT /") == (None, "Rs.10800/MT")
    assert parse_rate("Rs. 5 per meter") == (None, "Rs.5/meter")
    assert parse_rate("charges") == (None, "charges")
    assert parse_rate("") == (None, None)


def test_fta_rates():
    got = fta_rates("CN_FTA=0; MY_FTA=0; SAFTA_NLDC[13]=  5;  LK_FTA (Concession %) = 100%; ")
    assert got == {"fta:CN_FTA": (0.0, None), "fta:MY_FTA": (0.0, None), "fta:SAFTA_NLDC[13]": (5.0, None),
                   "fta:LK_FTA (Concession %)": (100.0, None)}


def test_editions_and_dates():
    # FY2022-23: linked from neither tariff page, read from FBR's Budget 2022-23 site
    assert EDITIONS[2023] == ["https://www.fbr.gov.pk/Budget2022-23/CustomsSchedulesFinal/Pakistan-Customs-Tariff.pdf"]
    assert sorted(EDITIONS) == list(range(2009, max(EDITIONS) + 1)) and max(EDITIONS) >= 2027
    assert str(in_force_from(2027)) == "2026-07-01"
