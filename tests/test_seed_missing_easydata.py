"""seed_missing_easydata_catalog: insert-only catalog rows for configured EasyData series (DB)."""
from __future__ import annotations

from dataclasses import replace

import pytest

from tests.test_api_integration import _db_available

pytestmark = pytest.mark.skipif(not _db_available(), reason="no database reachable")

NEW_ID = "external.goods_services_credit_test_seed"


def test_inserts_missing_rows_inactive_and_never_touches_existing_ones(monkeypatch):
    from app import db
    from db import init
    from ingestion import easydata_config

    real = {s.id: s for s in easydata_config.load_series()}
    base = real["external.goods_services_credit_2"]  # BOP 1949-64 (PKR): a configured series
    existing = db.query_one("SELECT id, name FROM series WHERE easydata_dataset_code IS NOT NULL ORDER BY id LIMIT 1")
    configured = [
        replace(base, id=NEW_ID),                                         # no catalog row yet
        replace(base, id=NEW_ID + "_spacer", name="."),                   # SBP's '.' spacer row
        replace(real.get(existing["id"], base), id=existing["id"], name="a name the config would impose"),
    ]
    monkeypatch.setattr(easydata_config, "load_series", lambda: configured)
    try:
        assert init.seed_missing_easydata_catalog(commit=False) == [NEW_ID]
        assert db.query_one("SELECT 1 FROM series WHERE id = %s", (NEW_ID,)) is None  # dry run writes nothing

        assert init.seed_missing_easydata_catalog(commit=True) == [NEW_ID]
        row = db.query_one("SELECT is_active, easydata_dataset_code, source_url FROM series WHERE id = %s", (NEW_ID,))
        assert row["is_active"] is False and row["easydata_dataset_code"] == "TS_GP_ES_PKBPM4964_Y"
        assert row["source_url"].endswith("/TS_GP_ES_PKBPM4964_Y.BOPR49640001/data")
        assert db.query_one("SELECT 1 FROM series WHERE id = %s", (NEW_ID + "_spacer",)) is None
        # an existing row keeps the name it has (disambiguation / units passes are never undone)
        assert db.query_one("SELECT name FROM series WHERE id = %s", (existing["id"],))["name"] == existing["name"]
        assert init.seed_missing_easydata_catalog(commit=True) == []  # idempotent
    finally:
        db.execute("DELETE FROM series WHERE id IN (%s, %s)", (NEW_ID, NEW_ID + "_spacer"))
