from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.sbp_auctions import _normalize_tenor, parse_auctions_html, series_id

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "kibor_live.html")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_series_id_shape():
    assert series_id("tbill", "3m") == "rates.auction.tbill.3m.cutoff_yield"
    assert series_id("gis", "5y") == "rates.auction.gis.5y.cutoff_price"


def test_normalize_tenor():
    assert _normalize_tenor("1-M") == "1m"
    assert _normalize_tenor("10-Y") == "10y"
    assert _normalize_tenor("bad") is None


def test_parses_tbill_and_pib_cutoffs():
    recs = parse_auctions_html(_load(), date(2026, 7, 23))
    ids = {r.series_id for r in recs}
    # MTB (T-Bill) and Fixed-Rate PIB cut-off yields present
    assert "rates.auction.tbill.3m.cutoff_yield" in ids
    assert "rates.auction.pib.10y.cutoff_yield" in ids
    # all values are plausible rates/prices
    assert all(r.value is not None and r.value > 0 for r in recs)


def test_does_not_emit_kibor_or_rejected():
    recs = parse_auctions_html(_load(), date(2026, 7, 23))
    # only auction.* series, never kibor.*
    assert all(r.series_id.startswith("rates.auction.") for r in recs)
    # 'Bids Rejected' rows produce no numeric record
    assert all(isinstance(r.value, float) for r in recs)


def test_empty_page_raises():
    with pytest.raises(ValueError):
        parse_auctions_html("<html><body>nothing</body></html>", date(2026, 7, 23))


def test_undated_rows_only_emitted_on_change():
    from ingestion.jobs.sbp_auctions import drop_unchanged_undated
    from ingestion.framework import Record
    today = date(2026, 9, 24)
    sid = "rates.auction.gis.3y.cutoff_price"
    same = [Record(sid, today, 100.2842, {})]
    # unchanged undated cut-off -> dropped (no daily copy)
    assert drop_unchanged_undated(same, today, {sid: 100.2842}) == []
    # new value -> kept
    assert drop_unchanged_undated(same, today, {sid: 99.5}) == same
    # never stored -> kept
    assert drop_unchanged_undated(same, today, {}) == same
    # a row carrying a real page date is always kept
    dated = [Record(sid, date(2026, 7, 6), 100.2842, {})]
    assert drop_unchanged_undated(dated, today, {sid: 100.2842}) == dated


def test_sid_regex_matches_renamed_ids():
    from ingestion.jobs.sbp_auctions import _SID_RE, series_id
    m = _SID_RE.match(series_id("gis", "3y"))
    assert m and m.groups() == ("gis", "3y", "cutoff_price")


def test_derive_auctions_bounds():
    from ingestion.jobs import derive_auctions as da
    assert da._MIN_YIELD <= 11.5 <= da._MAX_YIELD
    assert not (da._MIN_YIELD <= 0.1215)


def test_derive_auctions_maps_only_cutoff_series():
    """Weighted-average-yield series share the tenor; they must not feed cutoff_yield."""
    from ingestion.jobs import derive_auctions as da
    rows = [
        {"id": "banking.cut_off_yield_10", "easydata_dataset_code": "TS_GP_BAM_SIRPIBS_AH",
         "name": "Pakistan Investment Bonds Cut-off Yield 10-Years"},
        {"id": "banking.weighted_average_yield_10", "easydata_dataset_code": "TS_GP_BAM_SIRPIBS_AH",
         "name": "Pakistan Investment Bonds Weighted Average Yield 10-Years"},
        {"id": "banking.cut_off_yield_3_2", "easydata_dataset_code": "TS_GP_BAM_SIRTBIL_AH",
         "name": "Treasury Bills' Cut-off Yield 3-Months"},
        {"id": "banking.weighted_average_yield_3_2", "easydata_dataset_code": "TS_GP_BAM_SIRTBIL_AH",
         "name": "Treasury Bills' Weighted Average Yield 3-Months"},
    ]
    assert da.map_sources(rows) == {
        "banking.cut_off_yield_10": ("pib", "10y"),
        "banking.cut_off_yield_3_2": ("tbill", "3m"),
    }


def test_derive_auctions_one_value_per_date():
    from ingestion.jobs import derive_auctions as da
    mapping = {"a.cut_off_10": ("pib", "10y"), "b.cut_off_10": ("pib", "10y")}
    obs = [
        {"series_id": "b.cut_off_10", "obs_date": "2000-12-14", "value": 13.98},
        {"series_id": "a.cut_off_10", "obs_date": "2000-12-14", "value": 13.97},
        {"series_id": "a.cut_off_10", "obs_date": "2000-12-30", "value": 0.1},  # glitch, dropped
    ]
    assert da.collect_points(mapping, obs) == {("pib", "10y"): [("2000-12-14", 13.97)]}
