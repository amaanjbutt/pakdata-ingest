"""Unit tests for the derived per-city cost-of-living index. Pure, no DB."""
from __future__ import annotations

from datetime import date

from ingestion.jobs.cost_of_living import compute_city_indices


def _rows(city, series_prices):
    """series_prices: {series_id: {date: price}} -> flat observation rows."""
    out = []
    for sid, pts in series_prices.items():
        for d, v in pts.items():
            out.append({"series_id": sid, "obs_date": d, "value": v, "city": city})
    return out


W0, W1, W2 = date(2025, 1, 2), date(2025, 1, 9), date(2025, 1, 16)


def test_index_rebases_to_100_and_averages_relatives():
    # Two items: wheat +20% and sugar +40% by W2 (relatives 120 and 140 -> mean 130).
    rows = _rows("karachi", {
        "commodities.wheat": {W0: 100.0, W1: 110.0, W2: 120.0},
        "commodities.sugar": {W0: 50.0, W1: 60.0, W2: 70.0},  # 100 -> 120 -> 140
    })
    out = compute_city_indices(rows, min_basket=2)
    pts = dict(out["karachi"]["points"])
    assert out["karachi"]["base"] == W0 and out["karachi"]["basket_n"] == 2
    assert pts[W0] == 100.0
    assert pts[W2] == 130.0  # mean(120, 140)


def test_items_without_a_base_price_are_excluded_from_the_basket():
    rows = _rows("lahore", {
        "commodities.a": {W0: 100.0, W1: 110.0},
        "commodities.b": {W1: 200.0},  # no base-week (W0) price -> not in basket
    })
    out = compute_city_indices(rows, min_basket=1)
    assert out["lahore"]["basket_n"] == 1
    assert dict(out["lahore"]["points"])[W1] == 110.0  # only item a counts


def test_city_below_min_basket_is_skipped():
    rows = _rows("quetta", {"commodities.a": {W0: 100.0, W1: 110.0}})
    assert compute_city_indices(rows, min_basket=5) == {}


def test_empty_input():
    assert compute_city_indices([]) == {}
