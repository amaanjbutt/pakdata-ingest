"""Unit tests for derived fund analytics (pure — no DB)."""
from __future__ import annotations

from datetime import date

from app.services.fund_analytics import compute_fund_analytics


def _steady_navs(months: int = 37, rate: float = 0.01):
    """month-end NAVs compounding at `rate`/month from 100."""
    navs = []
    nav, y, m = 100.0, 2023, 1
    for _ in range(months):
        navs.append({"obs_date": date(y, m, 28), "nav": round(nav, 6)})
        nav *= 1 + rate
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return navs


def test_analytics_steady_growth():
    a = compute_fund_analytics(_steady_navs())
    assert a["observations"] == 37
    # since inception = 1.01^36 - 1 ≈ 43.08%
    assert 42 < a["trailing_returns"]["since_inception"] < 44
    # 1y total = 1.01^12 - 1 ≈ 12.68%
    assert 12 < a["trailing_returns"]["1y"] < 13.5
    # 3y annualized ≈ 1.01^12 - 1 ≈ 12.68% (steady rate)
    assert 11 < a["annualized_returns"]["3y"] < 14
    assert a["cagr_since_inception"] is not None and a["cagr_since_inception"] > 10
    assert a["volatility_annualized"] is not None
    # strictly increasing -> no drawdown
    assert a["max_drawdown"] == 0.0


def test_drawdown_detected():
    navs = [
        {"obs_date": date(2024, 1, 31), "nav": 100.0},
        {"obs_date": date(2024, 2, 29), "nav": 120.0},
        {"obs_date": date(2024, 3, 31), "nav": 90.0},   # -25% from peak 120
        {"obs_date": date(2024, 4, 30), "nav": 110.0},
    ]
    a = compute_fund_analytics(navs)
    assert a["max_drawdown"] == -25.0
    assert a["trailing_returns"]["since_inception"] == 10.0


def test_empty_and_single():
    assert compute_fund_analytics([])["observations"] == 0
    assert compute_fund_analytics([])["cagr_since_inception"] is None
    one = compute_fund_analytics([{"obs_date": date(2024, 1, 1), "nav": 100.0}])
    assert one["observations"] == 1 and one["max_drawdown"] is None
