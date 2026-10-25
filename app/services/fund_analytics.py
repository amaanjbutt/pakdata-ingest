"""Derived fund analytics computed from the deep NAV history.

Counters the "raw data only — compute returns yourself" gap: trailing total
returns, annualized (CAGR) returns, since-inception CAGR, annualized volatility,
and max drawdown — all derived from `fund_navs`. Pure (no DB/IO), so it is unit-
testable and cacheable.
"""
from __future__ import annotations

import math
from datetime import date, timedelta


def _nav_asof(pts: list[tuple[date, float]], target: date) -> float | None:
    """The last NAV on or before `target` (pts sorted ascending by date)."""
    val = None
    for d, nav in pts:
        if d <= target:
            val = nav
        else:
            break
    return val


def compute_fund_analytics(navs: list[dict]) -> dict:
    """navs: rows each with `obs_date` (date) and `nav` (numeric). Order-agnostic.
    Returns trailing/annualized returns (%), since-inception CAGR (%), annualized
    volatility (%), and max drawdown (%). Metrics needing more history than is
    present are `null`."""
    pts = [
        (r["obs_date"], float(r["nav"]))
        for r in navs
        if r.get("nav") is not None and float(r["nav"]) > 0
    ]
    pts.sort(key=lambda x: x[0])

    empty = {
        "trailing_returns": {}, "annualized_returns": {}, "cagr_since_inception": None,
        "volatility_annualized": None, "max_drawdown": None,
        "first_date": None, "last_date": None, "observations": len(pts),
    }
    if len(pts) < 2:
        if pts:
            empty["first_date"] = empty["last_date"] = pts[0][0].isoformat()
        return empty

    first_date, first_nav = pts[0]
    last_date, last_nav = pts[-1]

    def total_return(days: int) -> float | None:
        base = _nav_asof(pts, last_date - timedelta(days=days))
        return round((last_nav / base - 1) * 100, 2) if base and base > 0 else None

    def annualized(days: int) -> float | None:
        base = _nav_asof(pts, last_date - timedelta(days=days))
        if not base or base <= 0:
            return None
        yrs = days / 365.25
        return round(((last_nav / base) ** (1 / yrs) - 1) * 100, 2)

    ytd_base = _nav_asof(pts, date(last_date.year, 1, 1))
    trailing = {
        "1m": total_return(30), "3m": total_return(91), "6m": total_return(182),
        "ytd": round((last_nav / ytd_base - 1) * 100, 2) if ytd_base and ytd_base > 0 else None,
        "1y": total_return(365), "3y": total_return(365 * 3), "5y": total_return(365 * 5),
        "since_inception": round((last_nav / first_nav - 1) * 100, 2),
    }
    annual = {"3y": annualized(365 * 3), "5y": annualized(365 * 5)}

    total_years = (last_date - first_date).days / 365.25
    cagr = (
        round(((last_nav / first_nav) ** (1 / total_years) - 1) * 100, 2)
        if total_years > 0.5 and first_nav > 0 else None
    )

    # Annualized volatility from month-end returns (daily NAVs are noisy/gappy).
    monthly: dict[tuple[int, int], float] = {}
    for d, nav in pts:  # ascending -> last write wins = month-end NAV
        monthly[(d.year, d.month)] = nav
    mvals = [monthly[k] for k in sorted(monthly)]
    rets = [mvals[i] / mvals[i - 1] - 1 for i in range(1, len(mvals)) if mvals[i - 1] > 0]
    vol = None
    if len(rets) >= 6:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        vol = round(math.sqrt(var) * math.sqrt(12) * 100, 2)

    peak = pts[0][1]
    mdd = 0.0
    for _, nav in pts:
        peak = max(peak, nav)
        mdd = min(mdd, nav / peak - 1)

    return {
        "trailing_returns": trailing,
        "annualized_returns": annual,
        "cagr_since_inception": cagr,
        "volatility_annualized": vol,
        "max_drawdown": round(mdd * 100, 2),
        "first_date": first_date.isoformat(),
        "last_date": last_date.isoformat(),
        "observations": len(pts),
    }
