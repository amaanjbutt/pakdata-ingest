"""The scale of EasyData datasets whose API unit is a bare currency (`usd`, `pkr`).

EasyData's metadata names the currency but not the scale, so ~2,800 series were stored as `usd` /
`pkr` although SBP publishes most of them in millions (a $6.2 bn bond stock read as "$6,156"). Each
entry is settled against SBP's own published table — the file, the line that states the scale, and
a value that matches ours — never inferred from magnitudes. `python -m scripts.apply_easydata_units`
writes them to `series.unit` (dry run first; `--commit` backs up the old units).
"""
from __future__ import annotations

# dataset code -> (unit, where SBP states the scale + the value that matches ours, date checked)
VERIFIED: dict[str, tuple[str, str, str]] = {
    "TS_GP_ED_EDSEC_Q": (
        "usd_mn",
        "https://www.sbp.org.pk/assets/document/sdds.pdf (Gross External Debt Position by Sector, "
        "headed '(Million US$)'): general government bonds and notes 6,156 at end-June 2026 = "
        "debt.external.bonds_notes_long_term_govt",
        "2026-10-26",
    ),
}

# units that carry no scale — the only ones this registry may replace
BARE = ("usd", "pkr")
