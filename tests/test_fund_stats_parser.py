from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.mufap_fund_stats import parse_expenses_html, parse_payouts_html

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
PAYOUTS = os.path.join(FIX, "mufap_payouts.html")
EXPENSES = os.path.join(FIX, "mufap_expenses.html")


def _load(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


# ---- payouts -----------------------------------------------------------------

def test_parses_payouts():
    rows = parse_payouts_html(_load(PAYOUTS))
    assert len(rows) > 300
    r = {x.fund_id: x for x in rows}[12768]
    assert r.payout_date == date(2026, 6, 29)
    assert r.per_unit == 1.0559
    assert r.ex_nav == 10.2552


def test_payout_values_sane():
    rows = parse_payouts_html(_load(PAYOUTS))
    assert all(r.fund_id > 0 for r in rows)
    assert all(r.per_unit is None or r.per_unit >= 0 for r in rows)
    assert all(r.payout_date is None or r.payout_date.year >= 2000 for r in rows)


def test_payouts_missing_table_raises():
    with pytest.raises(ValueError):
        parse_payouts_html("<html><body>nope</body></html>")


# ---- expense ratios ----------------------------------------------------------

def test_parses_expenses():
    rows = parse_expenses_html(_load(EXPENSES))
    assert len(rows) > 400
    r = {x.fund_id: x for x in rows}[12768]
    assert r.obs_date == date(2026, 7, 24)
    assert r.ter_mtd == 0.95
    assert r.ter_ytd == 0.96
    assert r.mf == 0.7


def test_expense_ratios_are_percentages():
    """Values are percentages. A handful of funds legitimately report a TER far
    above 100% (verified in source: ABL Islamic Asset Allocation, TER MTD 186.51)
    when fixed costs dwarf a shrunken asset base — so we bound generously and
    only assert non-negativity plus that the bulk sit in a normal range."""
    rows = parse_expenses_html(_load(EXPENSES))
    values = [v for r in rows for v in (r.ter_mtd, r.ter_ytd, r.mf, r.sm) if v is not None]
    assert values
    # a small negative appears in source too (expense rebate / rounding, e.g. -0.01)
    assert all(-5 < v < 1000 for v in values)
    normal = [v for v in values if 0 <= v < 100]
    assert len(normal) / len(values) > 0.99  # outliers are rare, not the norm


def test_expenses_missing_table_raises():
    with pytest.raises(ValueError):
        parse_expenses_html("<html><body>nope</body></html>")
