"""Derived layer — every formula checked against hand-computed values (no DB)."""
from datetime import date

import pytest

from ingestion.jobs import derive_indicators as di


def d(s):
    return date.fromisoformat(s)


def test_splice_priority_first_source_wins():
    pbs = [(d("2017-07-31"), 2.9), (d("2017-08-31"), 3.4)]
    sbp_new = [(d("2016-07-31"), 4.1), (d("2017-07-31"), 3.0)]
    old = [(d("1964-07-31"), 1.0), (d("2016-07-31"), 4.0)]
    assert di.splice(pbs, sbp_new, old) == [
        (d("1964-07-31"), 1.0), (d("2016-07-31"), 4.1), (d("2017-07-31"), 2.9), (d("2017-08-31"), 3.4)]


def test_step_asof_and_policy_history_switches_at_corridor():
    inputs = {
        "rates.reverse_repo": [(d("2014-11-17"), 9.5), (d("2015-05-25"), 7.0), (d("2015-09-14"), 6.5)],
        "rates.policy_target": [(d("2015-05-25"), 6.5), (d("2015-09-14"), 6.0)],
        "rates.policy": [],
    }
    ch = di.policy_changes(inputs)
    # reverse repo only BEFORE 25-May-2015 (after it, the reverse repo is the corridor ceiling)
    assert ch == [(d("2014-11-17"), 9.5), (d("2015-05-25"), 6.5), (d("2015-09-14"), 6.0)]
    assert di.step_asof(ch, [d("2014-01-31"), d("2015-04-30"), d("2015-06-30"), d("2015-10-31")]) == [
        (d("2015-04-30"), 9.5), (d("2015-06-30"), 6.5), (d("2015-10-31"), 6.0)]


def test_real_policy_rate_is_policy_minus_cpi():
    inputs = {
        "rates.reverse_repo": [], "rates.policy": [],
        "rates.policy_target": [(d("2025-05-06"), 11.0), (d("2025-12-16"), 10.5)],
        "inflation.cpi.national.yoy": [(d("2025-06-30"), 3.2), (d("2025-12-31"), 5.6)],
        "inflation.national_cpi_inflation_measure_year_year": [], "industry.general_cpi_inflation_measure_year_year_2": [],
    }
    real = dict(di.SPECS[2].compute(inputs))
    assert di.SPECS[2].id == "rates.real.policy"
    assert real[d("2025-06-30")] == pytest.approx(7.8)
    assert real[d("2025-12-31")] == pytest.approx(4.9)


def test_monthly_mean_needs_min_obs_and_kibor_spread_uses_rate_in_effect():
    pts = [(d("2025-01-02"), 12.0), (d("2025-01-03"), 12.2), (d("2025-02-03"), 11.0)]
    assert di.monthly_mean(pts, 2) == [(d("2025-01-31"), pytest.approx(12.1))]
    spread = di.difference_asof(pts, [(d("2025-01-03"), 12.0)])
    assert spread == [(d("2025-01-03"), pytest.approx(0.2)), (d("2025-02-03"), pytest.approx(-1.0))]


def test_rolling_sum_requires_consecutive_months():
    months = [(d(f"2025-{m:02d}-28" if m == 2 else f"2025-{m:02d}-{30 if m in (4, 6, 9, 11) else 31}"), 100.0 + m)
              for m in range(1, 13)]
    out = di.rolling_sum_months(months, 12)
    assert out == [(d("2025-12-31"), sum(100.0 + m for m in range(1, 13)))]
    gappy = [p for p in months if p[0].month != 6]
    assert di.rolling_sum_months(gappy, 12) == []


def test_import_cover_is_reserves_over_trailing_3m_mean_imports():
    res = [(d("2026-03-31"), 15_000.0)]
    imp = [(d("2026-01-31"), 4_800.0), (d("2026-02-28"), 5_000.0), (d("2026-03-31"), 5_200.0)]
    assert di.ratio_to_trailing_mean(res, imp, 3) == [(d("2026-03-31"), pytest.approx(3.0))]
    assert di.ratio_to_trailing_mean(res, imp[1:], 3) == []  # incomplete window


def test_weekly_yoy_matches_52_weeks_back_with_tolerance():
    pts = [(d("2025-06-26"), 100.0), (d("2026-06-25"), 110.0), (d("2026-07-03"), 121.0), (d("2025-07-02"), 110.0)]
    out = dict(di.yoy_weekly(pts))
    assert out[d("2026-06-25")] == pytest.approx(10.0)       # exactly 364 days
    assert out[d("2026-07-03")] == pytest.approx(10.0)       # 366 days back, within tolerance
    assert d("2025-06-26") not in out


def test_share_and_weekly_median():
    assert di.share_pct([(d("2026-03-31"), 2_500.0)], [(d("2026-03-31"), 50.0)], 1000.0) == [
        (d("2026-03-31"), pytest.approx(5.0))]
    rows = [(f, d("2026-09-07"), 10.0 + f) for f in range(5)] + [(0, d("2026-09-10"), 30.0)]
    # week ending Fri 11-Sep (dated by its latest observation, 10-Sep): fund 0's latest value is 30 -> median of [30, 11, 12, 13, 14] = 13
    assert di.weekly_median(rows, 5) == [(d("2026-09-10"), 13.0)]
    assert di.weekly_median(rows[:4], 5) == []


def test_every_spec_has_method_inputs_and_unique_id():
    specs = di.SPECS + di.city_specs(["national", "karachi"]) + di.basket_specs(["national", "karachi"])
    ids = [s.id for s in specs]
    assert len(ids) == len(set(ids))
    for s in specs:
        assert s.method and s.inputs and s.unit and s.frequency


def test_jevons_yoy_is_geometric_mean_of_52_week_relatives():
    base, now = d("2025-07-03"), d("2026-07-02")  # 364 days apart
    items = {"a": [(base, 100.0), (now, 121.0)], "b": [(base, 50.0), (now, 50.0)],
             "c": [(now, 10.0)]}  # no year-ago price -> left out
    out = dict(di.jevons_yoy(items, min_items=2))
    assert out[now] == pytest.approx((((1.21 * 1.0) ** 0.5) - 1) * 100)  # 10%
    assert di.jevons_yoy(items, min_items=3) == []


def test_display_input_labels_non_series_inputs():
    assert di.display_input("rates.kibor.3m|side=offer") == "rates.kibor.3m"
    assert di.display_input("spi:karachi").startswith("commodities.*")
    assert di.display_input("funds:Money Market:d30") == "MUFAP fund returns (Money Market, d30)"


def test_basket_cost_sums_quantities_and_carries_a_missing_week():
    basket = {"a": 2, "b": 10}
    items = {
        "a": [(d("2026-01-01"), 100.0), (d("2026-01-08"), 110.0), (d("2026-01-15"), 120.0)],
        # b skips 01-08 (carried forward) and is gone after 01-15 (carry window ends)
        "b": [(d("2026-01-01"), 5.0), (d("2026-01-15"), 6.0)],
    }
    assert di.basket_cost(items, basket) == [
        (d("2026-01-01"), 250.0),   # 2*100 + 10*5
        (d("2026-01-08"), 270.0),   # 2*110 + 10*5 (b carried)
        (d("2026-01-15"), 300.0),   # 2*120 + 10*6
    ]
    stale = {"a": items["a"] + [(d("2026-02-12"), 130.0)], "b": items["b"]}
    assert d("2026-02-12") not in dict(di.basket_cost(stale, basket))  # b last seen 4 weeks earlier
    assert di.basket_cost({"a": items["a"]}, basket) == []            # an item with no prices at all
