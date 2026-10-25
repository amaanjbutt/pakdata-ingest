"""data_quality: isolated AMIS fair-quality-price spikes go to quarantine; real moves stay."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.test_api_integration import _db_available  # noqa: F401

needs_db = pytest.mark.skipif(not _db_available(), reason="needs the dev database")


@needs_db
def test_isolated_agri_spike_is_quarantined_but_a_level_shift_is_not():
    from app import db
    from ingestion.jobs.amis_prices import upsert_prices, upsert_reference
    from ingestion.jobs.data_quality import _DDL, DataQualityJob

    for ddl in _DDL:
        db.execute(ddl)
    db.execute("DELETE FROM agri_prices WHERE commodity_id IN (990002, 990003)")
    db.execute("DELETE FROM agri_prices_quarantine WHERE commodity_id IN (990002, 990003)")
    upsert_reference({990002: "Dqmandi"}, {990002: ("Dq Mint", "Vegetables"), 990003: ("Dq Onion", "Vegetables")})
    d0 = date.today() - timedelta(days=10)
    typo = [2500, 2500, 27002, 2500, 2500]            # one-day x10 typo
    shift = [1000, 1000, 5000, 5000, 5000]            # a real (lasting) jump
    rows = []
    for cid, series in ((990002, typo), (990003, shift)):
        rows += [{"commodity_id": cid, "market_id": 990002, "obs_date": d0 + timedelta(days=i),
                  "min": None, "max": None, "fqp": v, "quantity": None} for i, v in enumerate(series)]
    upsert_prices(rows)
    try:
        n = DataQualityJob()._quarantine_agri()
        left = {(r["commodity_id"], float(r["fqp"])) for r in db.query(
            "SELECT commodity_id, fqp FROM agri_prices WHERE commodity_id IN (990002, 990003)")}
        q = db.query("SELECT commodity_id, fqp, reason FROM agri_prices_quarantine WHERE commodity_id = 990002")
        assert n >= 1
        assert (990002, 27002.0) not in left and [float(r["fqp"]) for r in q] == [27002.0]
        assert (990003, 5000.0) in left                # the lasting jump is untouched
    finally:
        db.execute("DELETE FROM agri_prices WHERE commodity_id IN (990002, 990003)")
        db.execute("DELETE FROM agri_prices_quarantine WHERE commodity_id IN (990002, 990003)")
        db.execute("DELETE FROM agri_commodities WHERE commodity_id IN (990002, 990003)")
        db.execute("DELETE FROM agri_markets WHERE market_id = 990002")
