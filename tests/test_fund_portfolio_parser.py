"""Unit tests for mufap_fund_portfolio.parse_portfolio — pure, no DB/network.

Regression coverage for the two real bugs found 2026-08-17: (1) `Commodity` (gold)
has an amount but no percent field and was dropped, so gold/commodity funds showed
~14% instead of ~100%; (2) funds with Total=0 produced garbage (−billions) percents.
"""
from __future__ import annotations

import json

from ingestion.jobs.mufap_fund_portfolio import parse_portfolio


def _payload(table2_row: dict) -> str:
    # Mirrors the real envelope: outer {"data": "<json str with Table2>"}.
    return json.dumps({"data": json.dumps({"Table2": [table2_row]})})


def test_gold_fund_includes_commodity_and_sums_to_100():
    # Real Meezan Gold (12840) Table2 values.
    row = {
        "Date": "2026-07-01T00:00:00",
        "Cash": 1309.8434,
        "OtherIncludingReceivable": 51.9899,
        "Laibilities": -27.0383,
        "Commodity": 8350.9538,
        "Total": "9685.75",
    }
    as_of, alloc = parse_portfolio(_payload(row))
    assert str(as_of) == "2026-07-01"
    d = dict(alloc)
    assert "Commodity / Gold" in d and 85 < d["Commodity / Gold"] < 87
    assert d["Liabilities"] < 0  # liabilities net out negative
    assert 99.0 <= sum(d.values()) <= 101.0


def test_zero_total_fund_is_skipped_not_garbage():
    # Alfalah Equity (12679): Total=0 -> can't compute; skip rather than emit garbage.
    row = {
        "Date": "2026-07-01T00:00:00",
        "Cash": 3.111,
        "StocksOREquities": 877.69,
        "OtherInvestAmountFundOfFund": -878.6216,
        "Total": "0.00",
    }
    as_of, alloc = parse_portfolio(_payload(row))
    assert alloc == []


def test_normal_equity_fund_percents_from_amounts():
    row = {
        "Date": "2026-06-30T00:00:00",
        "Cash": 100.0,
        "StocksOREquities": 880.0,
        "OtherIncludingReceivable": 20.0,
        "Total": "1000.0",
    }
    _, alloc = parse_portfolio(_payload(row))
    d = dict(alloc)
    assert d["Equities"] == 88.0
    assert d["Cash"] == 10.0
    assert abs(sum(d.values()) - 100.0) < 0.01


def test_unreconciled_allocation_is_dropped():
    # Source reports only a fraction of holdings (bulk missing) -> sum ~28% -> drop
    # rather than store a misleading partial breakdown.
    row = {
        "Date": "2026-07-01T00:00:00",
        "Cash": 280.0,
        "OtherIncludingReceivable": 280.0,
        "Total": "1000.0",  # 72% of the fund is in an unreported field
    }
    _, alloc = parse_portfolio(_payload(row))
    assert alloc == []


def test_negative_blowup_allocation_is_dropped():
    # A huge negative fund-of-funds line makes the sum nonsensical (~-108%) -> drop.
    row = {
        "Date": "2026-07-01T00:00:00",
        "Cash": 20.0,
        "OtherInvestAmountFundOfFund": -150.0,
        "OtherIncludingReceivable": 58.0,
        "Total": "100.0",
    }
    _, alloc = parse_portfolio(_payload(row))
    assert alloc == []


def test_no_table2_returns_empty():
    assert parse_portfolio(json.dumps({"data": json.dumps({})})) == (None, [])
