from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.pbs_cpi_monthly import ALL_SERIES, parse_cpi_pdf

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pbs_cpi_historical.pdf")


def _recs():
    with open(FIXTURE, "rb") as f:
        return parse_cpi_pdf(f.read())


def test_all_eight_series_present_and_balanced():
    recs = _recs()
    by = {}
    for r in recs:
        by.setdefault(r.series_id, []).append(r)
    assert set(by) == set(ALL_SERIES)
    # every series covers the same months
    counts = {len(v) for v in by.values()}
    assert len(counts) == 1 and counts.pop() > 100


def test_index_and_rate_sections_are_not_conflated():
    """The PDF stacks index levels and YoY rates using an identical row shape.
    Levels sit ~100-300, rates ~-2 to +40; if the sections were mixed the ranges
    would overlap and the series would be silently corrupt."""
    recs = _recs()
    levels = [r.value for r in recs if r.series_id == "cpi.national"]
    rates = [r.value for r in recs if r.series_id == "cpi.national.yoy"]
    assert min(levels) > 100          # an index, never a rate
    assert max(rates) < 60            # a rate, never an index
    assert min(levels) > max(rates)   # the two ranges must not overlap


def test_known_values():
    recs = _recs()
    by = {(r.series_id, r.obs_date): r.value for r in recs}
    # first month in the file
    assert by[("cpi.national", date(2017, 7, 31))] == 107.1
    assert by[("cpi.urban", date(2017, 7, 31))] == 107.7
    assert by[("cpi.rural", date(2017, 7, 31))] == 106.3
    assert by[("wpi", date(2017, 7, 31))] == 105.3


def test_inflation_peak_is_historically_plausible():
    """Pakistan's headline YoY inflation peaked around 38% in 2023."""
    rates = [(r.obs_date, r.value) for r in _recs() if r.series_id == "cpi.national.yoy"]
    peak_date, peak = max(rates, key=lambda t: t[1])
    assert 35 <= peak <= 40
    assert peak_date.year == 2023


def test_observations_are_month_ends():
    assert all(r.obs_date.day >= 28 for r in _recs())


def test_garbage_raises():
    with pytest.raises(Exception):
        parse_cpi_pdf(b"%PDF-1.4 not a real pdf")
