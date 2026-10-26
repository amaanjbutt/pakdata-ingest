from __future__ import annotations

import json
import os
import re
from decimal import Decimal

from ingestion.easydata_units import (
    BARE, PER_UNIT, PER_UNIT_DATASETS, SCALE, SPLICED, UNVERIFIED, VERIFIED, load_splices, scaled_unit,
)
from scripts.apply_easydata_units import plan_splices, plan_units

_JSON = os.path.join(os.path.dirname(__file__), "..", "ingestion", "easydata_series.json")


def _series() -> list[dict]:
    with open(_JSON, encoding="utf-8") as f:
        return json.load(f)["series"]


def _code(s: dict) -> str:
    return s.get("easydata_dataset_code") or s["easydata_key"].split(".")[0]


def test_every_entry_is_sourced_and_scaled():
    for code, (unit, evidence, checked) in VERIFIED.items():
        assert unit in SCALE, code
        assert "http" in evidence and "=" in evidence, code  # the file, and the value that matches ours
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", checked), code
        # exchange rates are per-unit prices, never a scale
        assert not code.startswith("TS_GP_ER_") and code not in PER_UNIT_DATASETS and code not in UNVERIFIED


def test_scaled_unit():
    assert scaled_unit("TS_GP_BOP_SCRA_D", "bop.inflows_scra_equity_overall", "usd") == "usd_th"
    assert scaled_unit("TS_GP_BAM_CENGOVTD_M", "banking.central_government_debt_total", "pkr") == "pkr_bn"
    # per-share / per-person / per-US$ series keep the bare currency
    assert scaled_unit("TS_GP_FSA_NFCPSX05_Y", "corporate.basic_earnings_share_non_financial_listed_co",
                       "pkr") == "pkr"
    # already scaled, unverified or rate datasets are left alone
    assert scaled_unit("TS_GP_BOP_SCRA_D", "x", "usd_mn") == "usd_mn"
    assert scaled_unit("TS_GP_RLS_SALEFMCG_Q", "industry.fmcg.sales_fmcgs", "pkr") == "pkr"
    assert scaled_unit("TS_GP_ER_FAERPKR_M", "fx.rate.avg.aed", "pkr") == "pkr"
    assert scaled_unit(None, "prices.grocery_basket.lahore", "pkr") == "pkr"


def test_catalog_json_carries_the_verified_units():
    series = _series()
    ids = {s["id"] for s in series}
    left = [s["id"] for s in series if _code(s) in VERIFIED and s["unit"] in BARE and s["id"] not in PER_UNIT]
    assert left == []
    assert set(PER_UNIT) <= ids


def test_splices_match_the_registry():
    series = {s["id"]: s for s in _series()}
    rows = load_splices()
    assert len(rows) == 650
    assert len({(r["series_id"], r["obs_date"]) for r in rows}) == len(rows)
    for r in rows:
        pair = (r["dataset"], r["merged_from"])
        assert pair in SPLICED, pair
        assert r["series_id"] not in PER_UNIT and Decimal(r["value"]) != 0
        assert _code(series[r["series_id"]]) == r["dataset"]
    for (code, src), factor in SPLICED.items():
        # the factor takes the merged history's scale to the series' scale
        assert factor == SCALE[VERIFIED[src][0]] / SCALE[VERIFIED[code][0]], (code, src)


def test_plan_units_skips_per_unit_and_unknown():
    rows = [
        {"id": "debt.central_government_domestic_debt", "unit": "pkr", "easydata_dataset_code": "TS_GP_BAM_CENGOVTD_M"},
        {"id": "social.capita_health_expenditure", "unit": "pkr", "easydata_dataset_code": "TS_GP_RL_HLTHEXPNS_Y"},
        {"id": "industry.fmcg.sales_fmcgs", "unit": "pkr", "easydata_dataset_code": "TS_GP_RLS_SALEFMCG_Q"},
        {"id": "fx.rate.interbank.usd", "unit": "pkr", "easydata_dataset_code": None},
    ]
    assert plan_units(rows) == [("debt.central_government_domestic_debt", "pkr", "pkr_bn")]


def test_plan_splices_only_touches_points_still_unscaled():
    splices = [
        {"series_id": "fiscal.federal_govt_current_expenditures", "obs_date": "2010-06-30", "value": "2017255.0",
         "dataset": "TS_GP_PF_FGE21_Y", "merged_from": "TS_GP_PF_FGE0510_Y"},
        {"series_id": "fiscal.federal_govt_current_expenditures", "obs_date": "2009-06-30", "value": "1649223.0",
         "dataset": "TS_GP_PF_FGE21_Y", "merged_from": "TS_GP_PF_FGE0510_Y"},
        {"series_id": "corporate.cash_bank_balance_non_financial_listed_compa_2", "obs_date": "2004-12-31",
         "value": "160857.0", "dataset": "TS_GP_FSA_NFCPSX05_Y", "merged_from": "TS_GP_FSA_NFCPSX04_Y"},
    ]
    held = {
        ("fiscal.federal_govt_current_expenditures", "2010-06-30"): Decimal("2017255.0"),
        ("fiscal.federal_govt_current_expenditures", "2009-06-30"): Decimal("1649.223"),  # already done
        ("corporate.cash_bank_balance_non_financial_listed_compa_2", "2004-12-31"): Decimal("160857.0"),
    }
    out = plan_splices(splices, held)
    assert [(s, d) for s, d, _, _ in out] == [("fiscal.federal_govt_current_expenditures", "2010-06-30"),
                                            ("corporate.cash_bank_balance_non_financial_listed_compa_2", "2004-12-31")]
    assert out[0][3] == Decimal("2017.255") and out[1][3] == Decimal("160857000")
