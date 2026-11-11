"""fbr_valuation_rulings: FBR's ruling list and the value tables of real ruling PDFs."""
from __future__ import annotations

import json
import os
from datetime import date

from ingestion.jobs.fbr_valuation_rulings import (
    Ruling,
    claude_ruling,
    is_origin,
    item_of,
    kind_of,
    link_supersessions,
    load_claude,
    list_form,
    norm_number,
    ocr_pages,
    parse_list,
    parse_ruling,
    parse_values,
    pdf_candidates,
    pdf_pages,
    supersedes_of,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _pages(fn: str):
    with open(os.path.join(FIX, fn), "rb") as f:
        return pdf_pages(f.read())


def test_list_form_carries_what_the_grid_posts():
    f = list_form()
    # without these FBR answers "Value cannot be null. Parameter name: source"
    assert f["columns[0][data]"] == "VALUATIONNumber" and f["order[0][column]"] == "0"
    assert f["search[value]"] == "" and int(f["length"]) >= 1000


def test_parse_list():
    payload = {"draw": "1", "recordsTotal": 2, "recordsFiltered": 2, "data": [
        {"VALUATIONID": 986, "VALUATIONNumber": "Valuation Ruling No. 2124/2026",
         "Title": "Determination of Customs Value of Glass Lid under section 25A of the Customs Act, 1969",
         "CreationDate": "/Date(1793127600000)/",
         "UploadedFile1": "https://download1.fbr.gov.pk/VALUATIONS/2026103011102322728VR2124.pdf", "Department": "Customs"},
        {"VALUATIONID": 457, "VALUATIONNumber": "", "Title": "Withdrawal of Valuation Ruling regading Wooden Clothes Hangers",
         "CreationDate": "/Date(1591038000000)/", "UploadedFile1": "https://download1.fbr.gov.pk/Valuations/withdrawal2961-2020.pdf"},
    ]}
    a, b = parse_list(json.dumps(payload))
    assert (a.ruling_id, a.number, a.kind, a.item) == (986, "2124/2026", "ruling", "Glass Lid")
    assert a.listed_on == date(2026, 10, 28)          # /Date()/ is midnight Pakistan time
    assert b.number is None and b.kind == "withdrawal"


def test_numbers_kinds_items():
    assert norm_number("Valuation Ruling No.2124/2026") == "2124/2026"
    assert norm_number("1647-2022") == "1647/2022"
    assert norm_number("1360 of 2019") == "1360/2019"
    assert norm_number("") is None
    assert kind_of("Public Notice 05/2023", "Classification of …") == "public_notice"
    assert kind_of("", "Determination of Customs Export Value of Mangoes (All Types)") == "export_ruling"
    assert kind_of("Publication Valuation Ruling 54/2025", "Determination of …") == "publication"
    assert item_of("Determination of Customs Values of Polyester Filament Yarn under section 25-A of the Customs Act") \
        == "Polyester Filament Yarn"


def test_ruling_2124_2026_text_layer():
    p = parse_ruling(986, _pages("fbr_valuation_ruling_2124-2026.pdf"), "text", {"2007/2025"}, None)
    assert p.status == "parsed" and p.rejected == 0
    assert p.supersedes == ["2007/2025"]               # printed "200712025" by the scanner's OCR
    assert p.dated is None                             # handwritten "28", which that OCR reads as "29"
    got = [(v.pct_code, v.weboc_code, v.origin, v.value, v.unit, v.basis) for v in p.values]
    assert got == [("70102000", "701020001000", "China", 1.6, "kg", "C&F"),
                   ("70102000", "701020001000", "Other Origins", 1.8, "kg", "C&F"),
                   ("70102000", "701020001100", "China", 1.75, "kg", "C&F"),
                   ("70102000", "701020001100", "Other Origins", 1.95, "kg", "C&F")]
    assert p.values[0].item == "Glass Lid without Rim for Cookware of all types (Including Pots, Pans etc.)"


def test_ruling_2123_2026_merged_origin_and_codes():
    rows, rejected, found = parse_values(_pages("fbr_valuation_ruling_2123-2026_p2.pdf"))
    assert found
    # one origin cell ("China") spans the table; each row's WeBOC codes give one value row each
    assert {r.origin for r in rows} == {"China"}
    assert [(r.weboc_code, r.value) for r in rows if r.item.startswith("Artificial Fruit")] == [
        ("670210001300", 7.5), ("670290001300", 7.5)]
    # "6.s" is not a number and "6702.9000.r000" not a code: rejected, never guessed
    assert rejected >= 1
    assert not any(r.weboc_code == "670290001000" for r in rows)


def test_known_codes_gate():
    rows, rejected, _ = parse_values(_pages("fbr_valuation_ruling_2123-2026_p2.pdf"), known_codes={"67021000"})
    assert {r.pct_code for r in rows} == {"67021000"}
    assert rejected >= 3


def test_supersession_readings():
    assert supersedes_of("This Ruling supersedes Valuation Ruling No. 2007/2025 dated 14.07.2025") == ["2007/2025"]
    assert supersedes_of("supersedes earlier Valuation Ruling No. 161112022 dated 11.03.2022", {"1611/2022"}) == ["1611/2022"]
    assert supersedes_of("supersedes earlier Valuation Ruling No. 161112022 dated 11.03.2022", {"999/2022"}) == []


def test_link_supersessions():
    rs = [Ruling(1, "2007/2025", None, "ruling", "", None, date(2025, 7, 14), ""),
          Ruling(2, "2124/2026", None, "ruling", "", None, date(2026, 10, 29), "")]
    assert link_supersessions(rs, {2: ["2007/2025"]}) == {1: (2, date(2026, 10, 29))}
    assert link_supersessions(rs, {1: ["2124/2026"]}) == {}     # a later ruling can't be superseded by an earlier one


def test_is_origin():
    assert is_origin("China") and is_origin("Other Origins") and is_origin("All Origin")
    assert is_origin("China, Taiwan, Indonesia") and is_origin("Malaysia/ Indonesia")
    assert not is_origin("(l) of Section") and not is_origin("") and not is_origin("2.39")


def test_ocr_tokens_become_word_boxes():
    pages = ocr_pages([{"page": 0, "width": 1700, "height": 2200, "tokens": [
        {"box": [[100, 100], [400, 100], [400, 130], [100, 130]], "text": "Other Origins", "score": 0.97}]}])
    words = pages[0]
    assert [w.text for w in words] == ["Other", "Origins"]
    assert words[0].x0 == 100 * 72 / 200 and words[1].x1 == 400 * 72 / 200


def test_claude_transcription_of_2124_2026():
    t = load_claude()[986]
    p = claude_ruling(986, t, {"2007/2025"}, {"70102000"})
    assert p.text_source == "claude" and p.status == "parsed"
    assert p.dated == date(2026, 10, 28)               # read off the page image, handwriting included
    assert p.supersedes == ["2007/2025"]
    assert {(v.weboc_code, v.origin, v.value, v.unit) for v in p.values} == {
        ("701020001000", "China", 1.6, "kg"), ("701020001000", "Other Origins", 1.8, "kg"),
        ("701020001100", "China", 1.75, "kg"), ("701020001100", "Other Origins", 1.95, "kg")}


def test_claude_rows_are_checked_like_parsed_ones():
    t = {"dated": "2025-07-14", "supersedes": ["Valuation Ruling No. 9/2020", "junk"], "rows": [
        {"item": "Widget", "variant": "Grade A", "pct_codes": ["7010.2000", "9999.9999"], "weboc_codes": [],
         "origin": "China", "value": 2.5, "currency": "USD", "unit": "KG", "basis": "C&F"},
        {"item": "Widget", "pct_codes": ["7010.2000"], "origin": "China", "value": 0, "unit": "kg"},
        {"item": "Widget", "pct_codes": ["7010.20"], "origin": "China", "value": 3, "unit": "kg"}]}
    p = claude_ruling(1, t, {"9/2020"}, {"70102000"})
    assert [(v.pct_code, v.item, v.unit) for v in p.values] == [("70102000", "Widget (Grade A)", "kg")]
    assert p.rejected == 3 and p.status == "partial" and p.supersedes == ["9/2020"]


def test_claude_letter_with_another_number_is_not_stored():
    t = {"number_printed": "1038/2017", "rows": [
        {"item": "Ciprofloxacin", "pct_codes": ["2933.5990"], "origin": "China", "value": 40, "unit": "kg"}]}
    p = claude_ruling(262, t, None, None, number="1036/2017")    # FBR linked the next ruling's PDF
    assert p.status == "no_values" and p.values == [] and p.rejected == 1
    assert claude_ruling(261, t, None, None, number="1038/2017").status == "parsed"
    assert claude_ruling(262, dict(t, listed_number_typo=True), None, None, number="1036/2017").values


def test_pdf_candidates_cover_fbrs_moved_files():
    c = pdf_candidates("https://download1.fbr.gov.pk/Valuations/valuation795-2016.pdf", "795/2016")
    assert "https://download1.fbr.gov.pk/Valuations/valuation795-2015.pdf" in c
    c = pdf_candidates("https://download1.fbr.gov.pk/VALUATIONS/2025813981246957VR.2020-MedicalItems,Equipments.pdf", "2020/2025")
    assert c[0].endswith("MedicalItems,Equipments.pdf")
    assert "https://download1.fbr.gov.pk/Valuations/2025813981246957VR.2020-MedicalItems,Equipments.pdf" in c
