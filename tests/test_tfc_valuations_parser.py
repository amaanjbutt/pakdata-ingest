from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.mufap_tfc_valuations import parse_tfc_pdf

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mufap_tfc_valuations.pdf")


def _load() -> bytes:
    with open(FIXTURE, "rb") as f:
        return f.read()


def test_parses_as_of_date():
    d, _ = parse_tfc_pdf(_load())
    assert d == date(2026, 7, 24)


def test_parses_instrument_rows():
    _, rows = parse_tfc_pdf(_load())
    assert len(rows) >= 20
    by = {r.code: r for r in rows}
    r = by["BAHL/TFC/300921"]
    assert "BANK AL-HABIB" in r.name
    assert r.traded_status == "Non-Traded"
    assert r.price == 100.8311


def test_rating_categories_assigned():
    _, rows = parse_tfc_pdf(_load())
    cats = {r.rating_category for r in rows if r.rating_category}
    # rows sit under rating buckets like 'GOVERNMENT GUARANTEED / AAA', 'RATED AA+'
    assert cats
    assert any("AAA" in c or "RATED" in c for c in cats)
    # every row should inherit a bucket
    assert all(r.rating_category for r in rows)


def test_prices_are_plausible_and_codes_unique():
    _, rows = parse_tfc_pdf(_load())
    assert all(20 < r.price < 500 for r in rows)  # quoted around par
    assert len({r.code for r in rows}) == len(rows)


def test_garbage_raises():
    with pytest.raises(Exception):
        parse_tfc_pdf(b"%PDF-1.4 not a real pdf")
