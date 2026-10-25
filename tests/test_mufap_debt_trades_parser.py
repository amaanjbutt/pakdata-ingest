"""MUFAP "Debt Instruments Daily Trading" PDFs (tab 45) — the mid-2026 layout."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from ingestion.jobs.mufap_debt_trades import parse_debt_trades

FIX = Path(__file__).parent / "fixtures" / "mufap_debt_trades_2026_06.pdf"


def test_new_layout_rows_both_bats_kinds_and_split_numbers():
    rows = parse_debt_trades(FIX.read_bytes())
    assert len(rows) == 6
    assert {r.bats for r in rows} == {"BATS", "Non-BATS"}  # "Non-Bats" in the PDF
    bop = next(r for r in rows if r.issue_name.startswith("The Bank of Punjab"))
    # printed "1 ,500.00" and "Unliisted" in the source
    assert (bop.trade_date, bop.volume, bop.value_mn, bop.price_pct, bop.listed) == (
        date(2026, 6, 29), 1500.0, 149.52, 100.0, "Unlisted")


def test_every_row_reconciles_volume_x_face_x_price_to_value():
    for r in parse_debt_trades(FIX.read_bytes()):
        implied = r.volume * r.face_value * r.price_pct / 100 / 1e6
        assert abs(implied - r.value_mn) <= 0.01 * r.value_mn + 0.01, r
