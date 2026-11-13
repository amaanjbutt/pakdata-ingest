from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.pbs_lsm import ALL_SERIES, parse_qim_xlsx

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pbs_qim_trend.xlsx")


def _recs():
    with open(FIXTURE, "rb") as f:
        return parse_qim_xlsx(f.read())


def test_all_series_present():
    recs = _recs()
    by = {}
    for r in recs:
        by.setdefault(r.series_id, []).append(r)
    assert set(by) == set(ALL_SERIES)
    assert all(len(v) > 100 for v in by.values())  # ~10 years of months


def test_index_range_is_plausible():
    idx = [r.value for r in _recs() if r.series_id == "lsm.qim"]
    # QIM is an index around 90-140 over the sample
    assert all(50 < v < 200 for v in idx)


def test_latest_qim_matches_published_value():
    """May 2026 QIM was published as 116.10."""
    by = {(r.series_id, r.obs_date): r.value for r in _recs()}
    assert by[("lsm.qim", date(2026, 5, 31))] == pytest.approx(116.10, abs=0.05)


def test_changes_are_percentages():
    """MoM/YoY are percents. The +73.7% YoY at Apr-2021 is genuine - it is the
    rebound off the Apr-2020 COVID-lockdown collapse (verified against source),
    so the YoY band must accommodate that base effect."""
    rec = _recs()
    mom = [r.value for r in rec if r.series_id == "lsm.qim.mom"]
    yoy = [r.value for r in rec if r.series_id == "lsm.qim.yoy"]
    assert mom and all(-60 < v < 60 for v in mom)
    assert yoy and all(-60 < v < 100 for v in yoy)
    # the extreme is the known COVID base effect, not a parse error
    peak_date, peak = max(((r.obs_date, r.value) for r in rec
                           if r.series_id == "lsm.qim.yoy"), key=lambda t: t[1])
    assert peak_date.year == 2021 and 70 < peak < 80


def test_observations_are_month_ends():
    assert all(r.obs_date.day >= 28 for r in _recs())


def test_garbage_raises():
    with pytest.raises(Exception):
        parse_qim_xlsx(b"not a workbook")


# --- finding the current workbook (PBS stopped updating Trend-sheet.xlsx after May 2026) ---

_FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def test_industry_page_links_the_current_qim_workbook():
    from ingestion.jobs.pbs_lsm import qim_link

    with open(os.path.join(_FIX, "pbs_industry_qim_excerpt.html"), encoding="utf-8") as f:
        url = qim_link(f.read())
    # the "Download QIM Series" link, not the Table-1/2/3 workbooks beside it
    assert url == "https://www.pbs.gov.pk/wp-content/uploads/2020/07/QIM-Series-Base-Year-2015-16-11.xlsx"


def test_media_library_newest_qim_upload():
    from ingestion.jobs.pbs_lsm import newest_qim_upload

    with open(os.path.join(_FIX, "pbs_qim_media.json"), encoding="utf-8") as f:
        url = newest_qim_upload(f.read())
    assert url.endswith("/QIM-Series-Base-Year-2015-16-11.xlsx")  # uploaded 2026-10-16
    assert newest_qim_upload("[]") is None
    assert newest_qim_upload("<html>not json</html>") is None


def test_page_without_a_qim_workbook_gives_none():
    from ingestion.jobs.pbs_lsm import qim_link

    assert qim_link('<a href="/wp-content/uploads/2020/07/Table-1-August-26.xlsx">Table 1</a>') is None


def test_renamed_workbook_parses_through_august_2026():
    """QIM-Series-Base-Year-2015-16-11.xlsx (16 Oct 2026): same layout, three more months, and
    April/May 2026 restated (114.71 → 114.62, 116.10 → 115.87)."""
    with open(os.path.join(_FIX, "pbs_qim_series_2026_08.xlsx"), "rb") as f:
        by = {(r.series_id, r.obs_date): r.value for r in parse_qim_xlsx(f.read())}
    assert by[("lsm.qim", date(2026, 8, 31))] == pytest.approx(113.19, abs=0.01)
    assert by[("lsm.qim", date(2026, 6, 30))] == pytest.approx(108.78, abs=0.01)
    assert by[("lsm.qim", date(2026, 5, 31))] == pytest.approx(115.87, abs=0.01)
