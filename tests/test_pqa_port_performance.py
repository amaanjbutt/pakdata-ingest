"""PQA annual port performance table (ingestion/jobs/pqa_port_performance.py). Offline. Fixture: the live page
on 2026-11-10 (FY2021-22 → FY2025-26)."""
from __future__ import annotations

import os
from datetime import date

from ingestion.jobs.pqa_port_performance import fy_end, parse
from scripts.vintages_pqa import issues_from

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pqa_port_performance_2026-11-10.html")


def _recs():
    with open(FIXTURE, encoding="utf-8") as fh:
        years, recs = parse(fh.read())
    return years, {(r.series_id, r.obs_date, tuple(sorted(r.dims.items()))): r.value for r in recs}


def test_years_totals_and_terminals():
    years, recs = _recs()
    assert years == [date(y, 6, 30) for y in range(2022, 2027)]
    fy22 = date(2022, 6, 30)
    cargo = "ports.qasim.cargo_annual"
    assert recs[(cargo, fy22, (("direction", "all"), ("level", "total")))] == 55_150_000  # GRAND TOTAL
    assert recs[(cargo, fy22, (("cargo", "containers"), ("direction", "all"), ("level", "cargo_type")))] == 15_580_000
    assert recs[(cargo, fy22, (("cargo", "dry_and_liquid"), ("direction", "export"), ("level", "cargo_type")))] == 2_014_000
    pibt = "Pakistan International Bulk Terminal (PIBT)"
    assert recs[(cargo, fy22, (("direction", "all"), ("level", "terminal"), ("terminal", pibt)))] == 8_182_000
    assert recs[(cargo, fy22, (("commodity", "Coal"), ("direction", "import"), ("level", "commodity"),
                               ("terminal", pibt)))] == 8_128_000
    # 'Clinker ((Exp)' is an export; MPT's total is 'TOTAL MPT', not a section sub-total
    assert recs[(cargo, date(2024, 6, 30), (("commodity", "Clinker"), ("direction", "export"), ("level", "commodity"),
                                            ("terminal", pibt)))] == 53_000
    mpt = "Marginal Wharves (multi-purpose)"
    assert recs[(cargo, fy22, (("direction", "all"), ("level", "terminal"), ("terminal", mpt)))] == 2_716_000
    assert recs[(cargo, fy22, (("commodity", "Rice"), ("direction", "export"), ("level", "commodity"),
                               ("terminal", mpt)))] == 891_000
    # FOTCO prints furnace oil twice ('Furnace Oil', 'Furance oil'): one point, added up
    fotco = "Fauji Oil Terminal (FOTCO)"
    assert recs[(cargo, fy22, (("commodity", "Furnace oil (HSFO)"), ("direction", "import"), ("level", "commodity"),
                               ("terminal", fotco)))] == 2_226_000 + 50_000
    assert recs[("ports.qasim.teu_annual", fy22, (("direction", "import"),))] == 643_000
    assert recs[("ports.qasim.teu_annual", fy22, (("direction", "all"),))] == 1_274_000
    assert recs[("ports.qasim.ships_annual", fy22, (("level", "total"),))] == 1691
    # a blank cell (FOTCO's ships in FY2024-25) writes nothing
    assert ("ports.qasim.ships_annual", date(2025, 6, 30), (("level", "terminal"), ("terminal", fotco))) not in recs


def test_vintage_issues_keep_only_new_or_changed_prints():
    with open(FIXTURE, encoding="utf-8") as fh:
        page = fh.read()
    restated = page.replace("<strong>55150</strong>", "<strong>55151</strong>")  # FY2021-22 grand total restated
    issues = issues_from([("a", date(2026, 1, 1), page), ("b", date(2026, 6, 1), page), ("c", date(2026, 9, 1), restated)])
    assert [i["issue"] for i in issues] == ["a", "c"]  # 'b' repeats every print
    (row,) = issues[1]["rows"]
    assert row["dims"] == {"level": "total", "direction": "all"} and row["values"] == {"2022-06-30": [55151, 0]}
    assert issues[0]["source"] == "pqa_performance" and fy_end("2014-15") == date(2015, 6, 30)
