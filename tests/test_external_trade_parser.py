from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.pbs_external_trade import (
    month_end,
    parse_trade_xlsx,
    prev_month,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pbs_export_2026-06.xlsx")
JUN = date(2026, 6, 30)


def _rows():
    with open(FIXTURE, "rb") as f:
        return parse_trade_xlsx(f.read(), "export", JUN)


# ---- month helpers -----------------------------------------------------------

def test_month_helpers():
    assert month_end(2026, 6) == date(2026, 6, 30)
    assert month_end(2024, 2) == date(2024, 2, 29)  # leap year
    assert prev_month(date(2026, 6, 30)) == date(2026, 5, 31)
    assert prev_month(date(2026, 1, 31)) == date(2025, 12, 31)


# ---- workbook parsing --------------------------------------------------------

def test_extracts_current_and_prior_month():
    rows = _rows()
    months = {r.obs_date for r in rows}
    assert months == {JUN, date(2026, 5, 31)}
    # same commodity set in both months
    jun = {r.commodity for r in rows if r.obs_date == JUN}
    may = {r.commodity for r in rows if r.obs_date == date(2026, 5, 31)}
    assert jun == may and len(jun) > 50


def test_grand_total_matches_source():
    jun = [r for r in _rows() if r.obs_date == JUN and "G R A N D" in r.commodity]
    assert len(jun) == 1
    assert jun[0].value_pkr_mn == 626686.0
    assert round(jun[0].value_usd_th) == 2251530


def test_commodity_detail_and_group():
    rice = [r for r in _rows() if r.obs_date == JUN and r.commodity == "1.RICE"][0]
    assert rice.unit == "M.T"
    assert rice.commodity_group == "FOOD GROUP"
    assert round(rice.value_usd_th) == 213729


def test_subitems_are_parent_qualified():
    names = {r.commodity for r in _rows()}
    assert "1.RICE / a) BASMATI" in names
    assert "1.RICE / b) OTHERS" in names


def test_groups_captured():
    groups = {r.commodity_group for r in _rows() if r.commodity_group}
    assert {"FOOD GROUP", "TEXTILE GROUP"} <= groups


def test_excludes_fiscal_year_cumulative_block():
    """The workbook stacks a 'JULY - JUNE, 2025-2026' cumulative block below the
    month. Its grand total (~8.45m Rs mn) must never be ingested as a month."""
    vals = [r.value_pkr_mn for r in _rows() if "G R A N D" in r.commodity]
    assert all(v is not None and v < 1_000_000 for v in vals)
    assert 8454431.0 not in vals


def test_flow_label_and_uniqueness():
    rows = _rows()
    assert all(r.flow == "export" for r in rows)
    keys = [(r.flow, r.commodity, r.obs_date) for r in rows]
    assert len(keys) == len(set(keys))


def test_garbage_raises():
    with pytest.raises(Exception):
        parse_trade_xlsx(b"not a workbook", "export", JUN)
