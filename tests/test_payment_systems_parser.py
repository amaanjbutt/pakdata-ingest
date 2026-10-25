"""SBP Payment Systems Review snapshot parser (Phase 6).

Offline test against a saved real PDF (Q3-FY26). Skips if pdfplumber isn't
installed (it's a container dependency, like the other PDF/xlsx parsers).
"""
from __future__ import annotations

import os
from datetime import date

import pytest

pdfplumber = pytest.importorskip("pdfplumber")

from ingestion.jobs.sbp_payment_systems import (
    ALL_SERIES, _fy_quarter_to_date, parse_annexures, parse_payment_systems,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sbp_ps_review_q3fy26.pdf")


def _records():
    with pdfplumber.open(FIXTURE) as pdf:
        return parse_payment_systems(pdf.pages)


def _annex():
    with pdfplumber.open(FIXTURE) as pdf:
        return parse_annexures(pdf.pages)


def test_fy_quarter_to_date():
    assert _fy_quarter_to_date(1, 26) == date(2025, 9, 30)   # Q1 FY26 = Jul-Sep 2025
    assert _fy_quarter_to_date(2, 26) == date(2025, 12, 31)  # Q2 FY26 = Oct-Dec 2025
    assert _fy_quarter_to_date(3, 26) == date(2026, 3, 31)   # Q3 FY26 = Jan-Mar 2026
    assert _fy_quarter_to_date(4, 26) == date(2026, 6, 30)   # Q4 FY26 = Apr-Jun 2026


def test_snapshot_dates_are_two_quarter_ends():
    dates = {r.obs_date for r in _records()}
    # Q3-FY26 report carries Q2 (Dec-25) and Q3 (Mar-26)
    assert date(2025, 12, 31) in dates
    assert date(2026, 3, 31) in dates


def test_infrastructure_values():
    by = {(r.series_id, r.obs_date): r.value for r in _records()}
    q3 = date(2026, 3, 31)
    assert by[("payments.atms", q3)] == 21377
    assert by[("payments.pos_machines", q3)] == 247836
    assert by[("payments.bb_agents", q3)] == 819397
    assert by[("payments.qr_merchants", q3)] == 2506772
    assert by[("payments.payment_cards", q3)] == 68.3          # million
    assert by[("payments.ecommerce_merchants", q3)] == 17554
    # prior quarter present too
    assert by[("payments.atms", date(2025, 12, 31))] == 20976


def test_payments_summary_volume_and_value():
    by = {(r.series_id, r.obs_date): r.value for r in _records()}
    q3 = date(2026, 3, 31)
    assert by[("payments.retail.volume", q3)] == 3697.1        # million
    assert by[("payments.retail.value", q3)] == 168.8          # PKR trillion
    assert by[("payments.prism.value", q3)] == 389.8
    assert by[("payments.retail.digital.volume", q3)] == 3414.5
    assert by[("payments.retail.otc.value", q3)] == 100.6


def test_every_configured_series_is_parsed():
    got = {r.series_id for r in _records()} | {r.series_id for r in _annex()}
    for s in ALL_SERIES:
        assert s.id in got, f"missing {s.id}"


def test_series_ids_unique():
    ids = [s.id for s in ALL_SERIES]
    assert len(ids) == len(set(ids))


def test_missing_snapshot_raises():
    class _P:
        def extract_text(self):
            return "nothing useful here"
    with pytest.raises(ValueError):
        parse_payment_systems([_P()])


# ---- annexure depth: 5-quarter history tables (A-2 cards, A-4 channels, A-15 Raast) ----

def test_annexures_span_five_quarters():
    dates = {r.obs_date for r in _annex()}
    for d in (date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30),
              date(2025, 12, 31), date(2026, 3, 31)):
        assert d in dates


def test_annexure_card_composition():
    by = {(r.series_id, r.obs_date): r.value for r in _annex()}
    q3 = date(2026, 3, 31)
    assert by[("payments.cards.credit", q3)] == 3133992
    assert by[("payments.cards.debit", q3)] == 57196731
    assert by[("payments.cards.social_welfare", q3)] == 7715356


def test_annexure_raast_p2p_and_bulk():
    by = {(r.series_id, r.obs_date): r.value for r in _annex()}
    q3 = date(2026, 3, 31)
    assert by[("payments.raast.p2p.volume", q3)] == 664.4
    assert by[("payments.raast.p2p.value", q3)] == 18884.2   # PKR bn
    assert by[("payments.raast.bulk.value", q3)] == 4063.6


def test_annexure_per_channel_retail():
    by = {(r.series_id, r.obs_date): r.value for r in _annex()}
    q3 = date(2026, 3, 31)
    assert by[("payments.channel.mobile.volume", q3)] == 665.0
    assert by[("payments.channel.pos.value", q3)] == 798.0
    assert by[("payments.channel.ecommerce.volume", q3)] == 16.4


def test_unreachable_landing_page_fails_the_run(monkeypatch):
    import pytest

    from ingestion.jobs.sbp_payment_systems import SbpPaymentSystemsJob

    job = SbpPaymentSystemsJob()

    def blocked(url, *a, **k):
        raise PermissionError("HTTP 403")

    monkeypatch.setattr(job, "http_get", blocked)
    with pytest.raises(PermissionError):
        job.fetch()
