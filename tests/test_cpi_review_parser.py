"""PBS Monthly Review on Price Indices — the months the historical CPI PDF hasn't caught up with."""
from __future__ import annotations

import os
from datetime import date

from ingestion.jobs.pbs_cpi_monthly import parse_review_pdf, review_links

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "pbs_cpi_review_2026_08.pdf")


def test_review_month_index_levels_and_yoy():
    with open(FIXTURE, "rb") as f:
        recs = {(r.series_id, r.obs_date): r.value for r in parse_review_pdf(f.read())}
    aug = date(2026, 8, 31)
    assert {d for _, d in recs} == {aug}
    # index levels: the "General" rows (300.50 / 296.24 / 306.92 / 350.77), at the historical 1 dp
    assert recs[("cpi.national", aug)] == 300.5
    assert recs[("cpi.urban", aug)] == 296.2
    assert recs[("cpi.rural", aug)] == 306.9
    assert recs[("wpi", aug)] == 350.8
    # YoY: Table 1.a's own 1-dp figures (11.15 in the text tables is 11.1 there)
    assert recs[("cpi.national.yoy", aug)] == 11.1
    assert recs[("cpi.urban.yoy", aug)] == 10.4
    assert recs[("cpi.rural.yoy", aug)] == 12.2
    assert recs[("wpi.yoy", aug)] == 11.8


def test_review_links_newest_first():
    html = '''cpidata1  = [
     {     month:"August 2026",
      review:"https://www.pbs.gov.pk/wp-content/uploads/2020/07/Monthly-Review-August-2026-1.pdf",
      annex:"https://x/annex.xlsx" },
     { month:"July 2026",
      review:"https://www.pbs.gov.pk/wp-content/uploads/2020/07/Monthly-Review-July-2026.pdf" }]'''
    links = review_links(html)
    assert [d for d, _ in links] == [date(2026, 8, 31), date(2026, 7, 31)]
    assert links[0][1].endswith("Monthly-Review-August-2026-1.pdf")


def test_review_links_include_the_standalone_latest_link():
    """PBS links the newest review on its own before the archive array catches up
    (25-Oct-2026: September2026 linked, array still ending at August)."""
    html = '''<li><a href="https://www.pbs.gov.pk/wp-content/uploads/2020/07/Monthly-Review-September2026.pdf">
    </a></li> cpidata1  = [ { month:"August 2026",
      review:"https://www.pbs.gov.pk/wp-content/uploads/2020/07/Monthly-Review-August-2026-1.pdf" }]'''
    links = review_links(html)
    assert [d for d, _ in links] == [date(2026, 9, 30), date(2026, 8, 31)]
    assert links[0][1].endswith("Monthly-Review-September2026.pdf")
    assert links[1][1].endswith("Monthly-Review-August-2026-1.pdf")  # the array wins for its own months
