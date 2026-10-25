from __future__ import annotations

import os
import re

import pytest

from ingestion.jobs.mufap_debt_prices import (
    parse_pkfrv_csv,
    parse_pkisrv_instruments_csv,
)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
PKFRV = os.path.join(FIX, "mufap_pkfrv_instruments.csv")
PKISRV = os.path.join(FIX, "mufap_pkisrv_instruments.csv")


def _load(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


# ---- floating-rate PIBs ------------------------------------------------------

def test_parses_pib_floating():
    rows = parse_pkfrv_csv(_load(PKFRV))
    assert len(rows) > 30
    assert all(r.security_type == "pib_floating" for r in rows)
    assert all(r.code.upper().startswith("PIB") for r in rows)
    r = {x.code: x for x in rows}["PIBFR10YQ2030-10-22"]
    assert r.price == 98.80
    assert r.net_change == 0.09


def test_pib_prices_plausible():
    rows = parse_pkfrv_csv(_load(PKFRV))
    # bond prices quoted around par
    assert all(r.price is None or 50 < r.price < 200 for r in rows)


# ---- GoP Ijara Sukuk ---------------------------------------------------------

def test_parses_sukuk():
    rows = parse_pkisrv_instruments_csv(_load(PKISRV))
    assert len(rows) >= 10
    assert all(r.security_type == "gis_sukuk" for r in rows)
    assert all(r.code.upper().startswith("GOPIS") for r in rows)
    # the issue-numbered source code normalises to the canonical identity
    r = {x.code: x for x in rows}["GOPIS-FRR-2026-10-06"]
    assert r.sbp_code == "GIS (FRR) -08"
    assert r.price == 99.58
    assert r.rate_type == "FRR"
    assert str(r.maturity_date) == "2026-10-06"


def test_sukuk_does_not_pick_up_the_yield_curve():
    """The PKISRV file's right-hand columns hold the PKISRV curve — instrument
    parsing must ignore them (that curve is handled by mufap_pkrv)."""
    rows = parse_pkisrv_instruments_csv(_load(PKISRV))
    assert all(r.code.upper().startswith("GOPIS") for r in rows)
    assert not any("month" in (r.code or "").lower() for r in rows)


# ---- robustness --------------------------------------------------------------

def test_empty_raises():
    with pytest.raises(ValueError):
        parse_pkfrv_csv("Bond Code,Price,Net Change\n")
    with pytest.raises(ValueError):
        parse_pkisrv_instruments_csv("FMA Code,SBP Code,Price,Change\n")


# ---- legacy formats + content dispatch ---------------------------------------

LEGACY_BROKER = os.path.join(FIX, "mufap_legacy_broker_quotes.csv")
LEGACY_REUTERS = os.path.join(FIX, "mufap_legacy_reuters.csv")
CURVE = os.path.join(FIX, "pkrv_2026-07-22.csv")


def test_legacy_broker_quotes_use_the_average_column():
    from ingestion.jobs.mufap_debt_prices import parse_legacy_broker_quotes

    rows = parse_legacy_broker_quotes(_load(LEGACY_BROKER))
    assert len(rows) >= 20
    by = {r.code: r for r in rows}
    # GOPISV-30-04-2025 -> canonical VRR code; price is the 'Average' column
    r = by["GOPIS-VRR-2025-04-30"]
    assert r.price == 100.79
    assert r.security_type == "gis_sukuk"
    assert r.sbp_code == "GIS (VRR)"


def test_legacy_reuters_uses_security_plus_maturity():
    from ingestion.jobs.mufap_debt_prices import parse_legacy_reuters

    rows = parse_legacy_reuters(_load(LEGACY_REUTERS))
    assert len(rows) >= 20
    by = {r.code: r for r in rows}
    # 'GOPIS-VRR' + maturity 9-Oct-24 -> canonical code; price is 'AVG. RATE'
    r = by["GOPIS-VRR-2024-10-09"]
    assert r.price == 100.18
    assert r.security_type == "gis_sukuk"


def test_both_legacy_schemes_produce_the_same_canonical_form():
    """The two legacy layouts name the same instruments differently; both must
    normalise to GOPIS-<VRR|FRR>-<YYYY-MM-DD> so history joins up."""
    from ingestion.jobs.mufap_debt_prices import (
        parse_legacy_broker_quotes,
        parse_legacy_reuters,
    )

    a = {r.code for r in parse_legacy_broker_quotes(_load(LEGACY_BROKER))}
    b = {r.code for r in parse_legacy_reuters(_load(LEGACY_REUTERS))}
    pattern = re.compile(r"^GOPIS-(VRR|FRR)-\d{4}-\d{2}-\d{2}$")
    assert all(pattern.match(c) for c in a | b)
    assert a & b  # overlapping instruments across the two eras


def test_content_dispatch_handles_every_layout():
    from ingestion.jobs.mufap_debt_prices import parse_debt_csv

    assert len(parse_debt_csv(_load(LEGACY_BROKER))) >= 20
    assert len(parse_debt_csv(_load(LEGACY_REUTERS))) >= 20
    assert len(parse_debt_csv(_load(PKISRV))) >= 10
    assert len(parse_debt_csv(_load(PKFRV))) >= 30


def test_dispatch_skips_yield_curve_files_quietly():
    """Curve files belong to mufap_pkrv — they must yield [] rather than raise,
    so a mixed backfill doesn't count them as failures."""
    from ingestion.jobs.mufap_debt_prices import parse_debt_csv

    assert parse_debt_csv(_load(CURVE)) == []


def test_all_three_sukuk_schemes_share_one_canonical_identity():
    """Legacy broker matrix, legacy Reuters export and the current issue-numbered
    layout all describe the same instruments. Every one must resolve to
    GOPIS-<VRR|FRR>-<YYYY-MM-DD> with rate_type and maturity as real fields."""
    from ingestion.jobs.mufap_debt_prices import (
        parse_legacy_broker_quotes,
        parse_legacy_reuters,
    )

    pattern = re.compile(r"^GOPIS-(VRR|FRR)-\d{4}-\d{2}-\d{2}$")
    for rows in (parse_pkisrv_instruments_csv(_load(PKISRV)),
                 parse_legacy_broker_quotes(_load(LEGACY_BROKER)),
                 parse_legacy_reuters(_load(LEGACY_REUTERS))):
        assert rows
        for r in rows:
            assert pattern.match(r.code), r.code
            assert r.rate_type in {"VRR", "FRR"}
            assert r.maturity_date is not None
            assert r.code == f"GOPIS-{r.rate_type}-{r.maturity_date:%Y-%m-%d}"


def test_strict_parser_rejects_legacy_layouts():
    """The current-layout parser must NOT swallow legacy rows - doing so stored
    broker quotes and Excel date serials in the sbp_code field."""
    with pytest.raises(ValueError):
        parse_pkisrv_instruments_csv(_load(LEGACY_BROKER))
    with pytest.raises(ValueError):
        parse_pkisrv_instruments_csv(_load(LEGACY_REUTERS))


def test_pkfrv_skips_excel_serial_prices():
    from ingestion.jobs.mufap_debt_prices import parse_pkfrv_csv
    text = "Bond Code,Price,Net Change\nPIBFR10YQ2030-10-22,98.81,0.02\nPIB-FRB-1,43251,0\n"
    rows = parse_pkfrv_csv(text)
    assert [r.code for r in rows] == ["PIBFR10YQ2030-10-22"]
