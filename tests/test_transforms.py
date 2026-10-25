"""Pure unit tests for app.services.transforms — no DB, always run."""
from __future__ import annotations

from datetime import date

import pytest

from app.services import transforms as T


def _rows(pairs, dims=None):
    return [{"obs_date": d, "value": v, "dims": dims or {}} for d, v in pairs]


def _vals(rows):
    return [r["value"] for r in rows]


def test_pct_change_period_over_period():
    rows = _rows([(date(2020, 1, 1), 100.0), (date(2020, 2, 1), 110.0), (date(2020, 3, 1), 121.0)])
    out = T.apply_transform(rows, "pct_change", "monthly")
    assert _vals(out) == pytest.approx([None, 10.0, 10.0])


def test_three_ma_trailing_window():
    rows = _rows([(date(2020, m, 1), float(m)) for m in range(1, 5)])
    out = T.apply_transform(rows, "3ma", "monthly")
    # first two lack a full 3-window; then means of (1,2,3) and (2,3,4)
    assert _vals(out) == pytest.approx([None, None, 2.0, 3.0])


def test_index_rebases_to_100_at_first_value():
    rows = _rows([(date(2020, 1, 1), 50.0), (date(2020, 2, 1), 75.0), (date(2020, 3, 1), 100.0)])
    out = T.apply_transform(rows, "index", "monthly")
    assert _vals(out) == pytest.approx([100.0, 150.0, 200.0])


def test_yoy_monthly_uses_one_year_lookback():
    pairs = [(date(2020, m, 1), 100.0) for m in range(1, 13)]
    pairs += [(date(2021, m, 1), 110.0) for m in range(1, 13)]
    out = T.apply_transform(_rows(pairs), "yoy", "monthly")
    first_2021 = next(r for r in out if r["obs_date"] == date(2021, 1, 1))
    assert first_2021["value"] == pytest.approx(10.0)
    # 2020 has no prior year in the window -> null
    assert all(r["value"] is None for r in out if r["obs_date"].year == 2020)


def test_mom_monthly():
    rows = _rows([(date(2020, 1, 1), 200.0), (date(2020, 2, 1), 210.0)])
    out = T.apply_transform(rows, "mom", "monthly")
    assert _vals(out) == pytest.approx([None, 5.0])


def test_dims_groups_are_transformed_independently():
    rows = (
        _rows([(date(2020, 1, 1), 100.0), (date(2020, 2, 1), 110.0)], dims={"side": "bid"})
        + _rows([(date(2020, 1, 1), 100.0), (date(2020, 2, 1), 200.0)], dims={"side": "offer"})
    )
    out = T.apply_transform(rows, "pct_change", "monthly")
    bid = [r["value"] for r in out if r["dims"] == {"side": "bid"}]
    offer = [r["value"] for r in out if r["dims"] == {"side": "offer"}]
    assert bid == pytest.approx([None, 10.0])
    assert offer == pytest.approx([None, 100.0])


def test_zero_base_is_null_not_divide_error():
    rows = _rows([(date(2020, 1, 1), 0.0), (date(2020, 2, 1), 5.0)])
    out = T.apply_transform(rows, "pct_change", "monthly")
    assert _vals(out) == [None, None]  # base 0 -> null, not ZeroDivisionError


@pytest.mark.parametrize("kind,freq", [("yoy", "annual"), ("mom", "daily"), ("mom", "quarterly")])
def test_validate_rejects_mismatched_frequency(kind, freq):
    with pytest.raises(T.TransformError):
        T.apply_transform([], kind, freq)


def test_unknown_transform_rejected():
    with pytest.raises(T.TransformError):
        T.apply_transform([], "bogus", "monthly")


def test_transformed_unit():
    assert T.transformed_unit("yoy", "pkr") == "percent"
    assert T.transformed_unit("index", "pkr") == "index"
    assert T.transformed_unit("3ma", "pkr") == "pkr"
