from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.mufap_fund_returns import parse_returns_html

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mufap_returns.html")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_parses_all_funds():
    rows = parse_returns_html(_load())
    assert len(rows) > 400
    assert all(isinstance(r.fund_id, int) and r.fund_id > 0 for r in rows)
    assert len({r.fund_id for r in rows}) == len(rows)  # unique


def test_return_columns_present():
    rows = parse_returns_html(_load())
    r = {x.fund_id: x for x in rows}[12768]
    assert r.obs_date == date(2026, 7, 23)
    assert r.rating == "AA+(f)"
    assert r.returns["ytd"] == 10.77
    assert r.returns["d365"] == 10.43
    assert set(r.returns) == {"ytd", "mtd", "d1", "d15", "d30", "d90", "d180", "d270", "d365"}


def test_all_return_values_numeric_or_none():
    rows = parse_returns_html(_load())
    for r in rows:
        for v in r.returns.values():
            assert v is None or isinstance(v, float)


def test_empty_page_raises():
    with pytest.raises(ValueError):
        parse_returns_html("<html><body>no table</body></html>")
