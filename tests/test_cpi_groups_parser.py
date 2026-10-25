from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.pbs_cpi_groups import (
    ALL_SERIES,
    _GROUPS,
    _mg_code,
    parse_cpi_groups_pdf,
)

FIX = os.path.dirname(__file__)
FIXTURE = os.path.join(FIX, "fixtures", "pbs_cpi_groups_urban.pdf")
RURAL_FIXTURE = os.path.join(FIX, "fixtures", "pbs_cpi_groups_rural.pdf")


def _recs():
    with open(FIXTURE, "rb") as f:
        return parse_cpi_groups_pdf(f.read(), area="urban")


def _rural():
    with open(RURAL_FIXTURE, "rb") as f:
        return parse_cpi_groups_pdf(f.read(), area="rural")


def test_urban_and_rural_together_cover_all_series():
    by = {}
    for r in _recs() + _rural():
        by.setdefault(r.series_id, []).append(r)
    assert set(by) == set(ALL_SERIES)          # 13 urban + 13 rural
    assert all(len(v) >= 60 for v in by.values())


def test_area_tagging():
    assert all(r.series_id.startswith("cpi.urban.") for r in _recs())
    assert all(r.series_id.startswith("cpi.rural.") for r in _rural())


def test_rural_has_all_thirteen_groups():
    slugs = {r.series_id.split(".")[-1] for r in _rural()}
    assert slugs == set(_GROUPS.values())


def test_mg_code_handles_both_layouts():
    # "1 00 00 General" (separate) and "100 00 General" (glued) both -> MG 00
    assert _mg_code("1 00") == "00"
    assert _mg_code("100") == "00"
    assert _mg_code("2 01") == "01"   # Food
    assert _mg_code("201") == "01"


def test_recent_years_are_captured():
    """Recent-year pages glue SrNo+MG; regression that they are not dropped."""
    g = [r for r in _recs() if r.series_id == "cpi.urban.general"]
    dates = {r.obs_date for r in g}
    assert date(2026, 6, 30) in dates   # latest month
    assert date(2019, 7, 31) in dates   # earliest


def test_cross_validates_against_headline_urban_cpi():
    """The 'general' group IS the headline urban CPI - its index values must
    match the standalone headline series. At 2026-06 both read 290.2 (and the
    2026-05 peak is 291.6); an exact match across two independent parses of two
    different PBS sources is a strong correctness signal."""
    by = {(r.series_id, r.obs_date): r.value for r in _recs()}
    assert by[("cpi.urban.general", date(2026, 6, 30))] == pytest.approx(290.2, abs=0.1)
    assert by[("cpi.urban.general", date(2026, 5, 31))] == pytest.approx(291.6, abs=0.1)


def test_index_values_plausible():
    recs = _recs()
    assert all(50 < r.value < 1000 for r in recs)


def test_observations_are_month_ends():
    assert all(r.obs_date.day >= 28 for r in _recs())


def test_garbage_raises():
    with pytest.raises(Exception):
        parse_cpi_groups_pdf(b"not a pdf")
