from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.commodities_config import load_items
from ingestion.jobs.pbs_spi_weekly import (
    _match_item,
    _recent_thursdays,
    parse_spi_pdf,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
JUL = os.path.join(FIX, "spi_annex_2026-07-16.pdf")
APR = os.path.join(FIX, "spi_annex_2026-04-16.pdf")


def _load(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# ---- real-fixture parsing (two different months; layout drifts between issues)

@pytest.mark.parametrize("path,expected_date", [
    (JUL, date(2026, 7, 16)),
    (APR, date(2026, 4, 16)),
])
def test_parses_week_ended_date(path, expected_date):
    d, _ = parse_spi_pdf(_load(path))
    assert d == expected_date


@pytest.mark.parametrize("path", [JUL, APR])
def test_full_city_panel_and_items(path):
    _, recs = parse_spi_pdf(_load(path))
    cities = {r.dims["city"] for r in recs}
    # all 17 SPI cities plus the derived national
    assert "national" in cities
    assert len(cities) == 18
    assert {"karachi", "lahore", "islamabad", "peshawar", "quetta", "khuzdar"} <= cities
    items = {r.series_id for r in recs}
    assert len(items) >= 28  # ~30 configured items, minus any absent in a week
    assert "commodities.wheat_flour" in items
    assert "commodities.petrol" in items


@pytest.mark.parametrize("path", [JUL, APR])
def test_national_is_mean_of_city_avgs(path):
    _, recs = parse_spi_pdf(_load(path))
    by = {}
    for r in recs:
        by.setdefault(r.series_id, {})[r.dims["city"]] = r.value
    cvals = by["commodities.sugar"]
    cities = {k: v for k, v in cvals.items() if k != "national"}
    expected = round(sum(cities.values()) / len(cities), 2)
    assert cvals["national"] == pytest.approx(expected, abs=0.01)


def test_known_values_july():
    _, recs = parse_spi_pdf(_load(JUL))
    by = {(r.series_id, r.dims["city"]): r.value for r in recs}
    assert by[("commodities.wheat_flour", "lahore")] == 2266.54
    assert by[("commodities.wheat_flour", "karachi")] == 2759.40
    assert by[("commodities.sugar", "karachi")] == 150.13


@pytest.mark.parametrize("path", [JUL, APR])
def test_all_prices_positive(path):
    _, recs = parse_spi_pdf(_load(path))
    assert recs
    assert all(r.value is not None and r.value > 0 for r in recs)


# ---- robustness --------------------------------------------------------------

def test_garbage_pdf_raises():
    with pytest.raises(Exception):
        parse_spi_pdf(b"%PDF-1.4 not really a pdf")


def test_html_soft404_raises():
    # PBS serves missing weeks as an HTML page; that must not parse to records.
    with pytest.raises(Exception):
        parse_spi_pdf(b"<html><body>404 not found</body></html>")


def test_item_alias_specificity():
    """'Cooked Beef at Hotel' must not be mistaken for the raw-beef series."""
    items = load_items()
    assert _match_item("Beef with Bone (Average Quality)", items).id == "commodities.beef"
    assert _match_item("Cooked Beef at Average Hotel", items) is None
    # the two vegetable-ghee variants resolve to distinct series
    assert _match_item("Vegetable Ghee DALDA/HABIB 2.5 kg Tin", items).id == \
        "commodities.vegetable_ghee_tin"


def test_recent_thursdays_are_thursdays():
    thu = _recent_thursdays(5, ref=date(2026, 7, 23))  # a Thursday
    assert thu[0] == date(2026, 7, 23)
    assert all(d.weekday() == 3 for d in thu)
    assert thu[1] == date(2026, 7, 16)


def test_drop_city_outliers_removes_misparsed_cells():
    from ingestion.jobs.pbs_spi_weekly import drop_city_outliers
    vals = {"lahore": 569.0, "karachi": 590.0, "quetta": 600.0, "peshawar": 575.0,
            "multan": 15.0, "bannu": 2.0, "sukkur": 612.0}
    kept = drop_city_outliers(vals)
    assert "multan" not in kept and "bannu" not in kept
    assert set(kept) == {"lahore", "karachi", "quetta", "peshawar", "sukkur"}


def test_drop_city_outliers_keeps_real_dispersion_and_small_samples():
    from ingestion.jobs.pbs_spi_weekly import drop_city_outliers
    real = {"a": 180.0, "b": 220.0, "c": 260.0, "d": 300.0, "e": 400.0}  # onion-style spread
    assert drop_city_outliers(real) == real
    tiny = {"a": 1.0, "b": 500.0}  # too few cities to judge
    assert drop_city_outliers(tiny) == tiny


def test_archive_entries_pick_the_annexure_link_and_absolutize():
    """PBS's price-statistics page lists its weekly SPI archive as a JS array; some
    entries swap `annexure` and `report`, some links are relative."""
    from datetime import date

    from ingestion.jobs.pbs_spi_weekly import archive_entries

    page = """
      { date: "20-06-2024", annexure: "wp-content/uploads/2020/07/SPI20Executive20Sumary.pdf", report: "https://www.pbs.gov.pk/wp-content/uploads/2020/07/SPI20Annex202620USCP_20.06.2024.pdf" },
      { date: "13-07-2023", annexure: "wp-content/uploads/2020/07/SPI_AnnexUSCP_13072023.pdf", report: "x/Report.pdf" },
      { date: "06-07-2023", report: "only-a-report.pdf" },
    """
    assert archive_entries(page) == [
        (date(2023, 7, 13), "https://www.pbs.gov.pk/wp-content/uploads/2020/07/SPI_AnnexUSCP_13072023.pdf"),
        (date(2024, 6, 20), "https://www.pbs.gov.pk/wp-content/uploads/2020/07/SPI20Annex202620USCP_20.06.2024.pdf"),
    ]


def test_incremental_fills_listed_weeks_released_off_thursday(monkeypatch):
    """PBS moves the release around holidays (Wed 13-Aug-2025, Wed 11-Mar-2026); those weeks
    never matched the Thursday file-name probe. Incremental runs now take the archive list."""
    import types
    from datetime import date, timedelta

    from ingestion.jobs import pbs_spi_weekly as m

    today = date.today()
    wed = today - timedelta(days=(today.weekday() - 2) % 7 or 7)        # last Wednesday
    old = today - timedelta(weeks=30)
    held = wed - timedelta(days=7)
    page = "".join(f'{{ date: "{d:%d-%m-%Y}", report: "/a/Annex_{d:%d.%m.%Y}.pdf" }},' for d in (old, held, wed))
    monkeypatch.setattr("httpx.get", lambda *a, **k: types.SimpleNamespace(text=page))
    job = m.PbsSpiWeeklyJob()
    monkeypatch.setattr(job, "_get", lambda url: b"%PDF fake")
    got = job._recent_listed({held})
    assert [f.when for f in got] == [wed]        # not the held week, not the 30-week-old one


# --- PBS media library: annexures the archive list links wrongly or not at all ---------------

def test_media_annexes_dates_from_file_names():
    import json

    from ingestion.jobs.pbs_spi_weekly import media_annexes

    with open(os.path.join(FIX, "pbs_media_annex.json"), encoding="utf-8") as f:
        got = {url.rsplit("/", 1)[-1]: (named, up) for named, up, url in media_annexes(json.load(f))}
    # CPI / monthly-price annexes, the census coding scheme and .xlsx copies are not SPI annexure PDFs
    assert not any(n.startswith(("CPI_", "SPI_Monthly", "Coding")) or n.endswith(".xlsx") for n in got)
    assert got["SPI_AnnexUSCP_26072023.pdf"] == (date(2023, 7, 26), date(2024, 1, 23))
    assert got["SPI_AnnexUSCP_070923.pdf"][0] == date(2023, 9, 7)        # two-digit year
    assert got["SPI_AnexUSCP_10082023.pdf"][0] == date(2023, 8, 10)      # "Anex": found by the "SPI" search
    assert got["SPI-AnnexUSCP12102023.pdf"][0] == date(2023, 10, 12)      # no separator
    assert got["SPI_AnnexUSCP_27092023_0.pdf"][0] == date(2023, 9, 27)
    assert got["ANNEX202620USCP2002.05.2024.pdf"][0] == date(2024, 5, 2)  # "%20" debris before the date
    assert got["Annex_31.07.202-5.pdf"] == (None, date(2025, 8, 1))       # mistyped year: undated
    assert got["Annex.pdf"] == (None, date(2025, 6, 19))


def test_media_missing_dates_by_header_and_skips_held_weeks(monkeypatch):
    """A file is fetched only for a week we lack, then dated by its own header; an undated upload
    is tried for the Thursday on or before its upload day."""
    from ingestion.jobs.pbs_spi_weekly import PbsSpiWeeklyJob

    pdf = _load(os.path.join(FIX, "spi_annex_2026-04-16.pdf"))
    items = [
        {"date": "2026-04-17T10:00:00", "source_url": "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Annex_16.04.2026.pdf"},
        {"date": "2026-04-17T11:00:00", "source_url": "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Annex.pdf"},
        {"date": "2026-04-10T10:00:00", "source_url": "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Annex_09.04.2026.pdf"},
    ]
    fetched = []
    job = PbsSpiWeeklyJob()
    monkeypatch.setattr(job, "_media_items", lambda: items)
    monkeypatch.setattr(job, "_get", lambda url: fetched.append(url) or pdf)
    out = job._media_missing({date(2026, 4, 9)}, limit=20)
    assert [f.when for f in out] == [date(2026, 4, 16)]  # one file per header week
    assert not any(u.endswith("09.04.2026.pdf") for u in fetched)  # 9 Apr is held: never fetched
    assert len(fetched) == 1  # the second copy of 16 Apr is skipped once that week is taken
    assert job._media_missing({date(2026, 4, 9), date(2026, 4, 16)}, limit=20) == []
