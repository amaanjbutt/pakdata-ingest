"""SBP Quarterly SME Finance Review parser (Phase 6). Offline; needs pdfplumber."""
from __future__ import annotations

import os
from datetime import date

import pytest

pdfplumber = pytest.importorskip("pdfplumber")

from ingestion.jobs.sbp_sme_finance import ALL_SERIES, parse_sme_finance

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sbp_sme_qfr_mar2026.pdf")


def _records():
    with pdfplumber.open(FIXTURE) as pdf:
        return parse_sme_finance(pdf.pages)


def test_three_quarter_columns():
    dates = {r.obs_date for r in _records()}
    assert date(2025, 3, 31) in dates
    assert date(2025, 12, 31) in dates
    assert date(2026, 3, 31) in dates


def test_profile_values():
    by = {(r.series_id, r.obs_date): r.value for r in _records()}
    mar26 = date(2026, 3, 31)
    assert by[("sme.outstanding", mar26)] == 853.94
    assert by[("sme.npl_ratio", mar26)] == 9.93
    assert by[("sme.borrowers", mar26)] == 312355
    assert by[("sme.share_pvt_sector", mar26)] == 7.63


def test_intra_number_space_artifact_is_repaired():
    # "Fixed Investment ... 4 20.47" must parse as 420.47, not 4 or 20.47
    by = {(r.series_id, r.obs_date): r.value for r in _records()}
    assert by[("sme.facility.fixed_investment", date(2026, 3, 31))] == 420.47


def test_sector_and_bank_and_islamic_sections_disambiguated():
    by = {(r.series_id, r.obs_date): r.value for r in _records()}
    q = date(2026, 3, 31)
    # "Islamic Banks" appears in section 4 (bank share) and section 5 (islamic)
    assert by[("sme.bank_share.islamic", q)] == 100.44
    assert by[("sme.islamic.banks", q)] == 100.11
    assert by[("sme.sector.manufacturing", q)] == 236.20


def test_every_series_present_and_unique():
    got = {r.series_id for r in _records()}
    for s in ALL_SERIES:
        assert s.id in got, f"missing {s.id}"
    ids = [s.id for s in ALL_SERIES]
    assert len(ids) == len(set(ids))


def test_no_tables_raises():
    class _P:
        def extract_tables(self):
            return []
    with pytest.raises(ValueError):
        parse_sme_finance([_P()])
