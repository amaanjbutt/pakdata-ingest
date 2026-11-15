"""The Fifth Schedule concessions, regulatory duty and additional customs duty parsers, on trimmed real FBR files."""
from __future__ import annotations

import os
import re

from ingestion.jobs.fbr_additional_duty import (
    APPENDIX_I_2013,
    APPENDIX_I_2015,
    EARLY,
    OILS,
    RULES,
    acd_for,
    early_acd,
    list_codes,
    rd_goods,
)
from ingestion.jobs.fbr_customs_concessions import dedupe, parse_pdf, parse_rate
from ingestion.jobs.fbr_customs_tariff import pdf_words
import pytest

from ingestion.jobs.fbr_regulatory_duty import (
    AMENDMENTS,
    RdEntry,
    amended_table,
    claude_entries,
    excepted,
    expand,
    load_claude,
    load_early,
    norm_code,
    parse_pages,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _read(fn: str) -> bytes:
    with open(os.path.join(FIX, fn), "rb") as f:
        return f.read()


# ---- Fifth Schedule ---------------------------------------------------------------------------

def test_fifth_schedule_rows_rates_and_conditions():
    rows, skipped = parse_pdf(_read("fbr_fifth_schedule_2026-27_pages.pdf"), 2027, "u")
    rows = dedupe(rows)
    by = {(r.serial, r.code): r for r in rows}
    trencher = by[("1", "87019220")]
    assert trencher.part == "I" and trencher.rate_pct == 0.0
    assert trencher.description.startswith("Agricultural Machinery")
    assert trencher.sub_item.startswith("(1). Tractor mounted trencher")
    assert by[("1", "87019320")].rate_pct == 0.0          # a second code under the same sub-item
    assert by[("1", "84186910")].rate_pct == 2.0          # milk chillers: 2%
    assert not any(r.serial == "150" for r in rows)       # "(up to 75 lbs and head / 150 meters)" is not a serial
    assert skipped["heading or chapter entry"] >= 1       # "Respective headings" entries are counted, not stored
    assert all(len(r.code) == 8 for r in rows)


def test_fifth_schedule_rate_cells():
    assert parse_rate(["0%"]) == (0.0, None)
    assert parse_rate(["0%", "0%"]) == (0.0, None)       # the same rate printed against each code
    assert parse_rate(["Nil"]) == (0.0, "Nil")
    assert parse_rate(["0%,", "3%,", "11%,", "15%"]) == (None, "0%, 3%, 11%, 15%")


def test_fifth_schedule_serial_rate_phrase_over_numbered_sub_items():
    # FY2021-22 Part I S. No. 39, tourism projects: one rate, printed as a phrase down the rate column
    # beside the first of 47 sub-items numbered "1", "2" … (not serials of their own)
    rows = dedupe(parse_pdf(_read("fbr_fifth_schedule_2021-22_p37-39.pdf"), 2022, "u")[0])
    s39 = [r for r in rows if r.serial == "39"]
    assert len(s39) > 80 and {r.rate_text for r in s39} == {"50% of the prevailing rate of customs duty"}
    assert not any(r.serial in ("40", "41", "47") for r in rows)
    kitchen = [r for r in s39 if r.sub_item.startswith("41 Other Kitchen")]
    assert {r.code for r in kitchen} >= {"87168090", "94032000"}
    assert next(r for r in rows if r.serial == "37").rate_pct == 11.0


def test_fifth_schedule_rate_on_a_code_less_first_line_and_lettered_groups():
    # FY2024-25 S. No. 21 (renewable energy), item "8. Following machinery … imported by manufacturing
    # units of Solar Cells …": its "0%" is printed once, on that code-less line, on a page where no code
    # sits beside a rate (the rate column comes from the page before); "(a) Solar Cell Manufacturing
    # Equipment" and the "(i). Crystal (Grower) Puller …" below it print none of their own
    rows = dedupe(parse_pdf(_read("fbr_fifth_schedule_2024-25_p18-21.pdf"), 2025, "u")[0])
    s21 = [r for r in rows if r.serial == "21"]
    assert len(s21) > 100 and all(r.rate_pct == 0.0 for r in s21)
    puller = next(r for r in s21 if r.code == "84798990" and r.sub_item.startswith("(i). Crystal"))
    assert puller.description.startswith("Following systems and items for dedicated use with")
    assert any(r.code == "38249999" and r.sub_item.startswith("(vi). Solar grade polysilicon") for r in s21)
    # FY2026-27 S. No. 21: "(b). Raw materials … 0%" — its (i) … (iv) below print no rate of their own
    rows = dedupe(parse_pdf(_read("fbr_fifth_schedule_2026-27_p11-12.pdf"), 2027, "u")[0])
    mc4 = next(r for r in rows if r.code == "85369090")
    assert (mc4.serial, mc4.rate_pct) == ("21", 0.0) and mc4.sub_item.startswith("(ii). MC4 Connectors")


# ---- regulatory duty --------------------------------------------------------------------------

def test_rd_sro_840_2021_entries():
    entries = parse_pages(list(pdf_words(_read("fbr_rd_sro_840-2021_p1-2.pdf"))))
    first = entries[0]
    assert (first.entry, first.description, first.rate_pct) == ("0101.2100", "Pure-bred breeding animals", 5.0)
    poultry = next(e for e in entries if e.entry == "01.05")
    assert poultry.rate_pct == 10.0 and poultry.description.startswith("Live poultry")
    assert any(e.entry == "03.07" and e.rate_pct == 10.0 for e in entries)


def test_rd_heading_entries_cover_their_codes_and_codes_override():
    codes = {"01051100", "01051200", "01059400", "01012100", "03071100"}
    got = expand([RdEntry("01.05", "Live poultry", 10.0, None), RdEntry("0105.9400", "Fowls", 20.0, None),
                  RdEntry("0101.2100", "Horses", 5.0, None), RdEntry("9999.9999", "misread", 5.0, None),
                  RdEntry("03.07", "Molluscs", None, None)], codes)
    assert {c: e.rate_pct for c, e in got.items()} == {"01051100": 10.0, "01051200": 10.0, "01059400": 20.0,
                                                       "01012100": 5.0}


def test_rd_entries_except_codes_their_description_names():
    codes = {"03043100", "03046200", "03049900", "55091100", "55091200", "55092100"}
    got = expand([RdEntry("03.04", "Fish fillets … (PCT code except 0304.6200).", 17.5, None),
                  RdEntry("55.09", "Yarn … (except PCT code 5509.1100, 5509.1200)", 0.8, None)], codes)
    assert set(got) == {"03043100", "03049900", "55092100"}
    assert excepted("Other printed matter (except PCT code PCT Code 4911.9100)") == ["49119100"]


def test_rd_value_bands_keep_each_rate():
    codes = {"85171219"}
    got = expand([RdEntry("8517.1219", "Other having C&F Value up to US$ 30 per set", None, "Rs.165/set"),
                  RdEntry("8517.1219", "Other having C&F Value above US$ 30 per set", None, "Rs.1,620/set")], codes)
    e = got["85171219"]
    assert e.rate_pct is None and e.rate_text.startswith("Rs.165/set (Other having C&F Value up to US$ 30")
    assert "Rs.1,620/set (" in e.rate_text


def test_rd_claude_transcriptions_keep_the_decimal_point():
    sros = load_claude()
    fy27 = {e.entry: e for e in claude_entries(2027, sros)}
    assert fy27["55.09"].rate_pct == 0.8 and fy27["5511.1000"].rate_pct == 1.6   # OCR read "08" and "16"
    assert fy27["48.13"].rate_pct == 20.0 and fy27["03.04"].description.startswith("Fish fillets")
    fy26 = {e.entry: e for e in claude_entries(2026, sros)}
    assert fy26["03.04"].rate_pct == 17.5 and fy26["0304.6200"].rate_pct == 28.0


def test_rd_amendments_in_force_on_1_july():
    sros = load_claude()
    fy20 = {e.entry: e for e in claude_entries(2020, sros)}
    fy21 = {e.entry: e for e in claude_entries(2021, sros)}
    assert "1601.0000" not in fy20                             # inserted as S. No. 82A by SRO 573(I)/2020
    assert (fy21["1601.0000"].rate_pct, fy21["1601.0000"].sro) == (20.0, "573(I)/2020")
    assert fy21["5402.3300"].rate_pct == 2.5                     # S. No. 185 substituted
    fy24 = {e.entry: e for e in claude_entries(2024, sros)}
    assert (fy24["2849.1000"].rate_pct, fy24["2849.1000"].sro) == (5.0, "775(I)/2023")
    assert fy24["5402.3300"].rate_pct == 5.0                     # S. No. 209A, SRO 2293(I)/2022
    # every "for the figure X" of the amendments matched the transcribed table (or this raised)
    assert set(AMENDMENTS) == {2021, 2024}


def test_rd_amendment_must_match_the_table_it_amends():
    base = {"rows": [{"sno": "1", "codes": ["0101.2100"], "rate": "5"}]}
    with pytest.raises(ValueError, match="reads '5'"):
        amended_table(base, [("9(I)/2020", [("rate", ["1"], "10", "20")])], {"9(I)/2020": {"rows": []}})
    with pytest.raises(ValueError, match="S. No. 2"):
        amended_table(base, [("9(I)/2020", [("omit", ["2"])])], {"9(I)/2020": {"rows": []}})


def test_norm_code_reads_ocr_fragments():
    assert norm_code(["0'1", "01", ".21", "00"]) == "0101.2100"
    assert norm_code(["03.07"]) == "03.07"
    assert norm_code(["0307.11"]) == "0307.11"
    assert norm_code(["O1O5"]) == "01.05"
    assert norm_code(["12"]) is None


# ---- additional customs duty ------------------------------------------------------------------

def test_acd_slab_rules_fy2022_23():
    r = RULES[2023]
    assert acd_for((3.0, None), "01012100", r, {}) == 2
    assert acd_for((16.0, None), "55169300", r, {}) == 2       # named exception in the 16% slab
    assert acd_for((16.0, None), "55161100", r, {}) == 4
    assert acd_for((20.0, None), "61091000", r, {}) == 6
    assert acd_for((90.0, None), "22030000", r, {}) == 7
    assert acd_for((None, "Rs.10800/MT"), OILS[2], r, {}) == 2  # edible oils, a specific duty
    assert acd_for((0.0, None), "01022110", r, {}) is None      # the 0% slab is exempt …
    assert acd_for((0.0, None), "72042100", r, {}) == 2         # … except PCT 72.04
    assert acd_for((3.0, None), "39011000", r, {}) is None      # polymers of ethylene: exempt


def test_acd_rules_read_from_the_scanned_sros_fy2019_22():
    r20, r21, r22 = RULES[2020], RULES[2021], RULES[2022]
    assert acd_for((0.0, None), "01022110", r20, {}) == 2       # 670(I)/2019: the 0% slab pays 2%
    assert acd_for((20.0, None), "61091000", r20, {}) == 7      # … and 20% and up 7%
    assert acd_for((3.0, None), "12091000", r20, {}) is None    # seeds for sowing ("1209.0000" = heading 12.09)
    assert acd_for((0.0, None), "01022110", r21, {}) is None    # 572(I)/2020: 0% slab exempt
    assert acd_for((None, "Rs.10800/MT"), "15071000", r21, {}) is None  # edible oils exempt till 30 Sep 2020
    assert acd_for((None, "Rs.10800/MT"), "15119010", r21, {}) == 2     # … but not this one: 2%
    assert acd_for((20.0, None), "61091000", r22, {}) == 6      # 845(I)/2021: 20% slab 6%
    assert acd_for((50.0, None), "87032111", r22, {}) is None   # cars up to 850cc
    assert acd_for((50.0, None), "87032191", r22, {}) == 7
    assert acd_for((11.0, None), "29152100", r22, {}) is None   # on the list of exempt codes
    assert RULES[2024].sro == "967(I)/2022"


def test_acd_code_lists_read_from_sro_1063_2026():
    words = [w[3] for page in pdf_words(_read("fbr_acd_sro_1063-2026.pdf"))
             for w in sorted(page, key=lambda w: (round(w[1] / 4), w[0]))]
    known = {"22030000", "22041000", "01031000", "12019000", "39011000", "15119020", "99999999"}
    got = {rate: list_codes(words, start, end, known) for start, end, rate in RULES[2027].lists}
    assert "01031000" in got[4]                 # 20% slab goods charged 4%
    assert {"12019000", "39011000"} <= set(got[2])
    assert {"22030000", "22041000"} <= set(got[6])
    r = RULES[2027]
    listed = {c: rate for rate, cs in got.items() for c in cs}
    assert acd_for((0.0, None), "39011000", r, listed) == 2     # 0% slab, but on the list
    assert acd_for((10.0, None), "10063010", r, listed) is None  # slabs under 20% pay none in FY2026-27
    assert acd_for((90.0, None), "22030000", r, listed) == 6


# The FY2013-14 tariff's "Addl CD Ref. 693-2006 App.I" marks (its rd_acd column), as loaded
TARIFF_2013_14_APP_I = """
40081110 40081910 40082110 40082910 40091120 40092120 40093120 40094120 40129020 40169930 48211010 57032020 57033020
68132010 70071111 70072111 70091010 70140010 73151910 73201010 82060010 83012010 83021010 83023010 83100020 84073110
84073210 84073310 84099110 84099120 84133030 84152010 84212310 84213110 84254900 84831012 84834012 84835010 84836091
84839020 84841022 85071010 85113010 85118030 85118040 85119010 85122010 85123010 85124010 85129010 85365021 85371010
85443011 85444221 87071000 87079010 87081020 87082110 87082931 87082941 87083020 87083031 87085020 87087010 87088010
87089120 87089210 87089310 87089410 87089930 87089940 87141010 87141020 87141030 90261020 90292011 90328910 91040020
94012010 94019010 96138010""".split()


def test_acd_appendix_i_read_from_sro_693_2006_and_609_2015():
    # 693(I)/2006 as consolidated on 29 Oct 2013, Appendix I's pages: the code column (footnote marks as "8[…]")
    printed = []
    for page in pdf_words(_read("fbr_acd_sro_693-2006_app1.pdf")):
        for x0, _, _, t in sorted(page, key=lambda w: (round(w[1] / 4), w[0])):
            if t.startswith("Appendix-II"):
                break
            m = re.fullmatch(r"\[?(\d{4}\.\d{4})\]?", t)
            if m and x0 < 130:
                printed.append(m.group(1).replace(".", ""))
    assert printed == APPENDIX_I_2013 and len(printed) == 80
    # 609(I)/2015 part (a): the codes it inserts in Appendix I (quoted, leaving out the vehicles named in entries)
    words = [w[3] for page in pdf_words(_read("fbr_acd_sro_609-2015.pdf"))
             for w in sorted(page, key=lambda w: (round(w[1] / 4), w[0]))]
    text = " ".join(words)
    part_a = text[re.search(r"\(a\) in Appendix-I\b", text).end():re.search(r"\(b\) in Appendix-II", text).start()]
    quoted = {c.replace(".", "") for c in re.findall(r"[\"“]\s?(\d{4}\.\d{4})", part_a)
              if not c.startswith(("8701", "8702", "8703", "8704", "8711"))}
    assert quoted == set(APPENDIX_I_2015) - set(APPENDIX_I_2013)
    assert re.search(r"heading 8511\.8040 and entry relating thereto in column \(2\), the following shall be "
                     r"substituted, namely:- .8511\.8030", part_a) and "85118040" not in APPENDIX_I_2015
    # the FY2013-14 tariff's marks: the same list but for 609(I)/2015's two codes, which it marks early, and
    # 8481.8020 (inserted by 491(I)/2009), which it leaves out
    assert set(TARIFF_2013_14_APP_I) ^ set(APPENDIX_I_2013) == {"85118030", "87141030", "84818020"}


def test_acd_fy2015_2019_rules_in_force_on_1_july():
    none: set[str] = set()
    assert early_acd("85118040", EARLY[2015], none) == (15, "693(I)/2006")     # CDI units, the code before 609
    assert early_acd("85118030", EARLY[2015], none) is None
    assert early_acd("01012100", EARLY[2015], none) is None                     # nothing else carries ACD
    assert early_acd("85118030", EARLY[2016], none) == (15, "693(I)/2006")     # 16% only from 1 Dec 2015
    assert early_acd("87141040", EARLY[2016], none) == (15, "693(I)/2006")
    r17 = EARLY[2017]
    assert early_acd("87082931", r17, none) == (11, "693(I)/2006")              # 484(I)/2016
    assert early_acd("73182220", r17, {"73182220"}) == (11, "693(I)/2006")     # Appendix I wins over RD goods
    assert early_acd("01012100", r17, none) == (1, "1178(I)/2015")
    assert early_acd("04031000", r17, {"04031000"}) is None                     # goods under 568(I)/2014
    for code in ("09041130", "12091000", "31021000", "85171210", "85291090"):
        assert early_acd(code, r17, none) is None
    assert early_acd("52010010", r17, none) == (1, "1178(I)/2015")             # cotton: clause (bb) from Jan 2017
    assert early_acd("52010010", EARLY[2018], none) is None
    assert early_acd("55064000", EARLY[2018], none) is None                     # 554(I)/2017, 1 July 2017
    r19 = EARLY[2019]
    assert early_acd("01012100", r19, none) == (2, "630(I)/2018")
    assert early_acd("04031000", r19, rd_goods(2019, {"04031000"})) == (2, "630(I)/2018")  # RD goods pay too
    assert early_acd("09041130", r19, none) == (2, "630(I)/2018")             # no longer a listed seed
    for code in ("09042120", "31021000", "52010010", "55064000", "85171211"):
        assert early_acd(code, r19, none) is None
    assert early_acd("85171290", r19, none) == (2, "630(I)/2018")             # only 8517.1211/1219 now
    assert early_acd("87082931", r19, none) == (11, "693(I)/2006")


def test_acd_1178_exempts_the_goods_under_rd_sro_568_that_year():
    codes = {"04031000", "72131000", "08052100", "69072111", "87032323", "07019000"}
    assert rd_goods(2017, codes) == {"04031000", "72131000"}      # 1190 (vehicles) and 808 (potato) aren't 568
    assert rd_goods(2018, codes) == {"04031000", "72131000", "08052100", "69072111"}   # + 555(I)/2017's rows
    assert rd_goods(2015, codes) == set()                          # no 1178 before 1 Dec 2015


def test_rd_fy2015_2018_rebuilt_from_the_sros_in_force_on_1_july():
    early = load_early()
    assert sorted(early) == [2015, 2016, 2017, 2018]

    def at(fy, code):
        return sorted((e.rate_pct, e.rate_text, e.sro) for e in early[fy] if e.entry == code)

    assert at(2015, "0403.1000") == [(5.0, None, "568(I)/2014")]        # 568(I)/2014 as issued: 5%
    assert at(2015, "3305.1000") == []                                   # 631(I)/2014 is dated 11 July 2014
    assert at(2015, "0701.9000") == [(25.0, None, "808(I)/2009")]       # ware potato
    assert at(2016, "8517.1210") == []                                   # 602(I)/2015 omits mobile phones
    assert at(2016, "3402.2000") == [(5.0, None, "601(I)/2015")]
    assert at(2016, "72.29") == [(20.0, None, "601(I)/2015")]            # 601: "for 15, 20"
    assert at(2017, "72.13") == [(15.0, None, "568(I)/2014")]           # 236(I)/2016's 30% lapsed on 30 June 2016
    assert at(2017, "7601.2000") == []
    assert at(2017, "8703.2323") == [(50.0, None, "1190(I)/2015"), (60.0, None, "1190(I)/2015")]  # new / used
    assert at(2017, "1001.1900") == []                                   # wheat: an amendment to 254 is missing
    assert at(2018, "1001.1900") == [(60.0, None, "854(I)/2016")]
    assert at(2018, "7303.0000") == [(12.5, None, "568(I)/2014")]       # 555(I)/2017 recodes 73.03
    rows = expand(early[2017], {"72131000", "72139100", "87032323", "07019000"})
    assert rows["72131000"].rate_pct == 15 and rows["07019000"].rate_pct == 25
    assert "50%" in rows["87032323"].rate_text and "60%" in rows["87032323"].rate_text
