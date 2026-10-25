from __future__ import annotations

import os

from ingestion.discover_easydata import (
    derive_id,
    map_frequency,
    map_unit,
    merge_entries,
    parse_dataset_meta,
    to_iso_date,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "easydata_dataset_meta.json")
DATASET_CODE = "TS_GP_BOP_WR_M"


def _meta() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_derive_id_uses_topic_and_name_slug():
    # New clean scheme: <topic>.<name-slug>. The WR dataset maps to 'remittances'.
    assert derive_id("TS_GP_BOP_WR_M", "TS_GP_BOP_WR_M.WR0010",
                     "Total Workers Remittances") == "remittances.total_workers_remittances"
    # falls back to the series-code suffix when no name is given
    assert derive_id("TS_GP_BOP_WR_M", "TS_GP_BOP_WR_M.WR0010") == "remittances.wr0010"


def test_unit_mapping():
    assert map_unit("Million USD", "Total remittances") == "usd_mn"
    assert map_unit("USD", "some rate") == "usd"
    assert map_unit("Number", "count of txns") == "count"
    assert map_unit("Percent", "share") == "percent"
    assert map_unit("Widgets", "x") == "widgets"  # raw fallback


def test_frequency_mapping():
    assert map_frequency("Monthly") == "monthly"
    assert map_frequency("Annual") == "annual"
    assert map_frequency("Yearly") == "annual"
    assert map_frequency("half-yearly,semester") == "half_yearly"
    assert map_frequency("as-needed") == "irregular"
    assert map_frequency("occasionally annual") == "irregular"


def test_to_iso_date():
    assert to_iso_date("2003-07-31") == "2003-07-31"
    assert to_iso_date("") is None


def test_parse_dataset_meta_fields():
    entries = parse_dataset_meta(_meta(), DATASET_CODE)
    assert len(entries) == 3
    first = entries[0]
    assert first["easydata_key"] == "TS_GP_BOP_WR_M.WR0010"
    # clean-id scheme: <topic>.<name-slug>; the WR dataset -> 'remittances', no gp_
    assert first["id"].startswith("remittances.")
    assert "gp_" not in first["id"]
    assert first["unit"] == "usd_mn"
    assert first["frequency"] == "monthly"
    assert first["backfill_start"] == "2003-07-31"
    assert first["module"] == "economic"
    assert first["easydata_dataset_code"] == DATASET_CODE
    assert first["easydata_last_refresh"] == "2026-07-10"
    # HTML stripped from description
    assert "<" not in first["description"]
    # Number-typed series maps to count
    assert entries[2]["unit"] == "count"


def test_merge_is_idempotent_and_curated_wins():
    entries = parse_dataset_meta(_meta(), DATASET_CODE)
    # A curated entry for WR0010 with a nicer id must survive discovery.
    curated = [{
        "easydata_key": "TS_GP_BOP_WR_M.WR0010",
        "id": "remittances.total",
        "module": "economic", "name": "Workers' Remittances — Total",
        "description": "", "unit": "usd_mn", "frequency": "monthly",
        "source": "SBP-EasyData", "tier": "basic",
        "min_value": 0, "max_value": 20000, "max_step": 5000,
        "backfill_start": "2000-01-01",
    }]
    merged, added = merge_entries(curated, entries)
    # WR0010 stays curated; WR0020 and WR0030 are added.
    assert added == 2
    wr0010 = [e for e in merged if e["easydata_key"] == "TS_GP_BOP_WR_M.WR0010"]
    assert len(wr0010) == 1 and wr0010[0]["id"] == "remittances.total"

    # Re-running merge adds nothing (idempotent).
    merged2, added2 = merge_entries(merged, entries)
    assert added2 == 0
    assert len(merged2) == len(merged)


def test_merge_keeps_ids_unique():
    a = [{"easydata_key": "A.1", "id": "dup.x"}]
    b = [
        {"easydata_key": "B.1", "id": "dup.x"},
        {"easydata_key": "C.1", "id": "dup.x"},
    ]
    merged, added = merge_entries(a, b)
    assert added == 2
    ids = [e["id"] for e in merged]
    assert len(ids) == len(set(ids))  # no duplicate ids
