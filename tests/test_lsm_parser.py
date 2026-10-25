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
