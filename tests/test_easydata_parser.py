from __future__ import annotations

import os
from datetime import date

from ingestion.jobs.easydata_sync import parse_easydata_json

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "easydata_remittances.json")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_maps_series_key_to_catalog_id():
    records = parse_easydata_json(_load())
    assert all(r.series_id == "remittances.total" for r in records)


def test_parses_dates_and_values():
    records = parse_easydata_json(_load())
    by_date = {r.obs_date: r.value for r in records}
    assert by_date[date(2022, 7, 31)] == 2523.753816
    assert by_date[date(2022, 8, 31)] == 2724.108


def test_empty_value_becomes_none():
    records = parse_easydata_json(_load())
    by_date = {r.obs_date: r.value for r in records}
    assert by_date[date(2022, 9, 30)] is None


def test_unknown_series_key_is_skipped():
    payload = (
        '{"columns": ["Series Key", "Observation Date", "Observation Value"],'
        ' "rows": [["NOT_IN_CONFIG.X", "2022-01-31", "1.0"]]}'
    )
    assert parse_easydata_json(payload) == []


def test_missing_column_raises():
    import pytest

    payload = '{"columns": ["Series Key", "Observation Date"], "rows": []}'
    with pytest.raises(ValueError):
        parse_easydata_json(payload)


# ---- payload shapes seen 2026-10-14 (data-quality audit) --------------------------------
from ingestion.jobs.easydata_sync import easydata_rows  # noqa: E402

_COLS = ('{"columns": ["Dataset Name", "Series Key", "Series Name", "Observation Date", "Observation Value", '
         '"Unit", "Observation Status", "Status Comments"], ')


def test_discontinued_key_returns_an_empty_row():
    # TS_GP_MFS_TDDCTCR_Q.DEPCC20 now answers "rows": [[]] — used to raise IndexError every run
    assert easydata_rows(_COLS + '"rows": [[]]}') == []


def test_unescaped_quotes_in_status_comments_fall_back_to_row_parsing():
    # SBP's CPI 12-month moving-average series: a free-text comment with bare quotes makes
    # the whole payload invalid JSON — every pull of those series failed
    text = (_COLS + '"rows": ['
            '["Inflation Snapshot (New Base: 2015-16)", "TS_GP_PT_CPI_M.P00161516", "National CPI; 12MMA", '
            '"2017-05-31", "", "Percent", "Missing value", "The observation status is missing "MIS" due to '
            'trimmed inflation"], '
            '["Inflation Snapshot (New Base: 2015-16)", "TS_GP_PT_CPI_M.P00161516", "National CPI; 12MMA", '
            '"2026-08-31", "7.53", "Percent", "Normal", ""]]}')
    assert easydata_rows(text) == [("TS_GP_PT_CPI_M.P00161516", "2017-05-31", ""),
                                   ("TS_GP_PT_CPI_M.P00161516", "2026-08-31", "7.53")]


def test_invalid_payload_that_is_not_easydata_still_raises():
    import json

    import pytest
    with pytest.raises(json.JSONDecodeError):
        easydata_rows('<html>not json')


def test_sweep_order_flagships_first_then_least_recently_checked():
    from datetime import datetime, timezone

    from ingestion.jobs.easydata_sync import _sweep_order
    t = lambda d: datetime(2026, 10, d, tzinfo=timezone.utc)  # noqa: E731
    datasets = {
        "forex_ds": _sweep_order(0, t(13)),          # flagship, checked yesterday: still first
        "prices_ds": _sweep_order(2, None),
        "scra_daily": _sweep_order(6, t(1)),         # external, unchecked for 13 days
        "debt_ds": _sweep_order(3, t(12)),
        "never_checked": _sweep_order(6, None),
    }
    assert sorted(datasets, key=datasets.get) == ["forex_ds", "prices_ds", "never_checked", "scra_daily", "debt_ds"]
