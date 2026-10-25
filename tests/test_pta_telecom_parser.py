from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.pta_telecom import INDICATORS, _label_to_date, parse_pta_chart

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(fn: str) -> str:
    with open(os.path.join(FIX, fn), encoding="utf-8") as f:
        return f.read()


def _recs(key: str, fn: str):
    return parse_pta_chart(_load(fn), INDICATORS[key])


# ---- date labels (the three PTA period formats) -----------------------------

def test_monthly_label():
    assert _label_to_date("Jul-25", "monthly") == date(2025, 7, 31)
    assert _label_to_date("Feb-26", "monthly") == date(2026, 2, 28)
    assert _label_to_date("garbage", "monthly") is None


def test_quarterly_label():
    assert _label_to_date("Jul-Sep 22", "quarterly") == date(2022, 9, 30)
    assert _label_to_date("Jan-Mar 26", "quarterly") == date(2026, 3, 31)
    assert _label_to_date("Apr-Jun 23", "quarterly") == date(2023, 6, 30)


def test_annual_fiscal_label():
    assert _label_to_date("2018-19 (R)", "annual") == date(2019, 6, 30)
    assert _label_to_date("2024-25 (P)", "annual") == date(2025, 6, 30)
    assert _label_to_date("2020-21", "annual") == date(2021, 6, 30)


# ---- teledensity (monthly, original coverage) -------------------------------

def test_teledensity_three_series():
    recs = _recs("teledensity", "pta_teledensity.html")
    ids = {r.series_id for r in recs}
    assert ids == {"telecom.teledensity.total", "telecom.teledensity.cellular",
                   "telecom.teledensity.fixed"}
    by = {(r.series_id, r.obs_date): r.value for r in recs}
    assert by[("telecom.teledensity.total", date(2026, 6, 30))] == 83.52


def test_teledensity_cellular_plus_fixed_approximates_total():
    by: dict[date, dict[str, float]] = {}
    for r in _recs("teledensity", "pta_teledensity.html"):
        by.setdefault(r.obs_date, {})[r.series_id] = r.value
    for vals in by.values():
        if len(vals) == 3:
            approx = vals["telecom.teledensity.cellular"] + vals["telecom.teledensity.fixed"]
            assert abs(approx - vals["telecom.teledensity.total"]) < 0.05


# ---- subscribers (monthly, multi-chart page: counts, not market share) ------

def test_subscribers_counts_not_market_share():
    recs = _recs("subscribers", "pta_subscribers.html")
    by = {(r.series_id, r.obs_date): r.value for r in recs}
    # first-match wins -> we get the subscriber-count chart (~208M total), not
    # the market-share chart (fractions < 1) that shares the same series names.
    total = by[("telecom.subscribers.cellular.total", date(2026, 6, 30))]
    assert total > 200_000_000
    jazz = by[("telecom.subscribers.cellular.jazz", date(2026, 6, 30))]
    assert jazz == 75421212
    # operator counts sum to the published total
    op_sum = sum(
        by[(f"telecom.subscribers.cellular.{op}", date(2026, 6, 30))]
        for op in ("jazz", "ufone", "zong", "telenor", "sco")
    )
    assert op_sum == total


# ---- revenues (annual fiscal-year) ------------------------------------------

def test_revenues_annual():
    recs = _recs("revenues", "pta_revenues.html")
    by = {(r.series_id, r.obs_date): r.value for r in recs}
    assert by[("telecom.revenue.total", date(2025, 6, 30))] == 1074.9
    # observations land on fiscal-year ends (30 June)
    assert all(r.obs_date.month == 6 and r.obs_date.day == 30 for r in recs)


# ---- cell sites (quarterly) -------------------------------------------------

def test_cell_sites_quarterly():
    recs = _recs("cell_sites", "pta_cell_sites.html")
    ids = {r.series_id for r in recs}
    assert ids == {"telecom.cell_sites.2g", "telecom.cell_sites.3g", "telecom.cell_sites.4g"}
    # observations land on quarter-ends
    assert all(r.obs_date.month in (3, 6, 9, 12) for r in recs)


# ---- guards -----------------------------------------------------------------

def test_missing_chart_raises():
    with pytest.raises(ValueError):
        parse_pta_chart("<html>no chart</html>", INDICATORS["teledensity"])


def test_all_series_ids_unique():
    from ingestion.jobs.pta_telecom import ALL_SERIES
    assert len(ALL_SERIES) == len(set(ALL_SERIES))


def test_fetch_raises_when_every_page_fails(monkeypatch):
    """A blocked source (PTA dropped the VPS from Aug-2026) must fail the run, not
    read as no_new_data — that hid two months of failures."""
    import pytest

    from ingestion.jobs.pta_telecom import PtaTelecomJob

    job = PtaTelecomJob()

    def boom(url, *a, **k):
        raise TimeoutError("connection timed out")

    monkeypatch.setattr(job, "http_get", boom)
    with pytest.raises(TimeoutError):
        job.fetch()
