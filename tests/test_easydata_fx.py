"""FX module is sourced from EasyData monthly exchange-rate datasets.

These are offline tests: they exercise the config mapping and the shared
EasyData JSON parser against a saved fixture (no network, no DB).
"""
from __future__ import annotations

import os
from datetime import date

from ingestion.easydata_config import by_easydata_key, load_series
from ingestion.jobs.easydata_sync import parse_easydata_json

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "easydata_fx_faerpkr.json")

_EXPECTED_FX_IDS = {
    f"fx.rate.{kind}.{ccy}"
    for kind in ("avg", "monthend")
    for ccy in ("usd", "eur", "gbp", "jpy", "sar", "aed", "cny")
} | {"fx.effective.reer", "fx.effective.neer"}


def test_all_fx_series_configured():
    # The clean-id rename unified all auto-discovered ER currencies into fx.*, so
    # the 16 headline series are now a subset of a broader fx.* namespace.
    ids = {s.id for s in load_series() if s.id.startswith("fx.")}
    assert _EXPECTED_FX_IDS <= ids


def test_headline_fx_series_are_monthly_easydata():
    by_id = {s.id: s for s in load_series()}
    for sid in _EXPECTED_FX_IDS:
        s = by_id[sid]
        assert s.frequency == "monthly"
        # user-facing source was renamed SBP-EasyData -> SBP (2026-08-19)
        assert s.source == "SBP"
        # dataset code lets the nightly incremental cheap-check its freshness
        assert s.easydata_dataset_code and s.easydata_dataset_code.startswith("TS_GP_ER_")


def test_currency_keys_map_to_clean_ids():
    m = by_easydata_key()
    assert m["TS_GP_ER_FAERPKR_M.E00220"].id == "fx.rate.avg.usd"
    assert m["TS_GP_ER_FMEERPKR_M.E00220"].id == "fx.rate.monthend.usd"
    assert m["TS_GP_ER_REERNEER_M.R00010"].id == "fx.effective.reer"
    assert m["TS_GP_ER_REERNEER_M.N00010"].id == "fx.effective.neer"


def test_parser_maps_er_rows_to_fx_series_and_skips_unmapped():
    with open(FIXTURE, encoding="utf-8") as f:
        records = parse_easydata_json(f.read())
    by = {(r.series_id, r.obs_date): r.value for r in records}
    # USD average, two months
    assert by[("fx.rate.avg.usd", date(2026, 5, 31))] == 281.4021
    assert by[("fx.rate.avg.usd", date(2026, 6, 30))] == 278.5795536
    # JPY average
    assert by[("fx.rate.avg.jpy", date(2026, 6, 30))] == 1.9231
    # the unmapped E99999 row is dropped, not errored
    assert all(not sid.endswith("e99999") for sid, _ in by)
    assert len(records) == 3
