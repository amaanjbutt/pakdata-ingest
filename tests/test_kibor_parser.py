from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.sbp_kibor import parse_market_snapshot, _normalize_tenor, parse_kibor_html

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "kibor_live.html")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_observation_date_is_the_kibor_as_on_date():
    # Dated by the page's own "KIBOR As on 22- Jul - 26" line (never an unrelated
    # 'as on' date from the auction tables), not by when we happened to fetch it.
    obs_date, _ = parse_kibor_html(_load(), date(2026, 7, 25))
    assert obs_date == date(2026, 7, 22)


def test_falls_back_to_fetch_date_without_as_on_line():
    html = _load().replace("KIBOR As on", "KIBOR rates")
    obs_date, _ = parse_kibor_html(html, date(2026, 7, 23))
    assert obs_date == date(2026, 7, 23)


def test_market_snapshot_fx_reserves_repo():
    recs = {(r.series_id, r.dims.get("side")): r for r in parse_market_snapshot(_load())}
    assert recs[("fx.rate.m2m.usd", None)].value == 277.9139
    assert recs[("fx.rate.interbank.usd", "bid")].value == 277.6293
    assert recs[("fx.rate.interbank.usd", "offer")].value == 278.0544
    assert recs[("fx.rate.m2m.usd", None)].obs_date == date(2026, 7, 22)
    assert recs[("reserves.liquid.sbp", None)].value == 17225.8
    assert recs[("reserves.liquid.banks", None)].value == 5449.7
    assert recs[("reserves.liquid.total", None)].value == 22675.5
    assert recs[("reserves.liquid.total", None)].obs_date == date(2026, 7, 10)
    assert recs[("rates.repo.overnight", None)].value == 11.72
    assert recs[("rates.repo.overnight", None)].obs_date == date(2026, 7, 21)


def test_market_snapshot_missing_blocks_never_raise():
    assert parse_market_snapshot("<html><body>nothing here</body></html>") == []


def test_parses_published_tenors_bid_and_offer():
    _, records = parse_kibor_html(_load(), date(2026, 7, 23))
    # 3 published tenors x (bid + offer) = 6 records
    series_ids = {r.series_id for r in records}
    assert series_ids == {"kibor.3m", "kibor.6m", "kibor.1y"}
    assert len(records) == 6


def test_bid_offer_values_from_live_page():
    _, records = parse_kibor_html(_load(), date(2026, 7, 23))
    by = {(r.series_id, r.dims["side"]): r.value for r in records}
    assert by[("kibor.3m", "bid")] == 11.42
    assert by[("kibor.3m", "offer")] == 11.67
    assert by[("kibor.1y", "bid")] == 11.44
    assert by[("kibor.1y", "offer")] == 11.94


def test_does_not_pick_up_auction_tables():
    # The page also has T-Bill/PIB/Sukuk 'Cut-off Yield' tables — none of those
    # rows should leak into KIBOR records.
    _, records = parse_kibor_html(_load(), date(2026, 7, 23))
    assert all(r.series_id.startswith("kibor.") for r in records)
    assert all(r.value is not None and 0 < r.value < 40 for r in records)


def test_normalize_tenor():
    assert _normalize_tenor("3-M") == "3m"
    assert _normalize_tenor("6-M") == "6m"
    assert _normalize_tenor("12-M") == "1y"
    assert _normalize_tenor("1-Year") == "1y"
    assert _normalize_tenor("garbage") is None


def test_missing_table_raises():
    with pytest.raises(ValueError):
        parse_kibor_html("<html><body>no rates here</body></html>", date(2026, 7, 16))
