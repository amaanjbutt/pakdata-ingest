from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.mufap_fund_navs import parse_nav_history, parse_nav_html

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mufap_navs.html")
HIST_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "mufap_nav_history.json")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_parses_all_funds():
    rows = parse_nav_html(_load())
    # ~538 funds in the daily snapshot
    assert len(rows) > 400
    # every fund has a stable numeric id and a name
    assert all(isinstance(r.fund_id, int) and r.fund_id > 0 for r in rows)
    assert all(r.name for r in rows)


def test_fund_ids_unique():
    rows = parse_nav_html(_load())
    ids = [r.fund_id for r in rows]
    assert len(ids) == len(set(ids))


def test_nav_values_and_dates():
    rows = parse_nav_html(_load())
    by_id = {r.fund_id: r for r in rows}
    # 786 Smart Fund (FundID 12814) from the saved snapshot
    f = by_id[12814]
    assert f.name == "786 Smart Fund"
    assert f.nav == 84.6125
    assert f.offer == 85.4586
    assert f.obs_date == date(2026, 7, 22)
    assert f.inception_date == date(2003, 5, 20)


def test_metadata_present():
    rows = parse_nav_html(_load())
    f = {r.fund_id: r for r in rows}[12814]
    assert f.amc == "786 Investments Limited"
    assert "Income" in f.category


def test_empty_page_raises():
    with pytest.raises(ValueError):
        parse_nav_html("<html><body>no table</body></html>")


# ---- NAV history (backfill) --------------------------------------------------

def test_parses_nav_history():
    with open(HIST_FIXTURE, encoding="utf-8") as f:
        series = parse_nav_history(f.read())
    assert len(series) >= 40
    # each point is (date, nav)
    d0, v0 = series[0]
    assert isinstance(d0, date) and isinstance(v0, float) and v0 > 0
    # dates are unique and NAVs plausible
    assert len({d for d, _ in series}) == len(series)
    assert all(v > 0 for _, v in series)


def test_nav_history_bad_json():
    assert parse_nav_history('{"data": "{\\"Table1\\": []}"}') == []


def test_clean_nav_series_drops_isolated_spikes_and_nonpositive():
    from datetime import date
    from ingestion.jobs.mufap_fund_navs import clean_nav_series
    d = lambda i: date(2024, 4, 20 + i)
    pts = [(d(0), 1170.0), (d(1), 1173.29), (d(2), 1.0), (d(3), 1187.88), (d(4), 0.0), (d(5), 1190.0)]
    out = clean_nav_series(pts)
    assert (d(2), 1.0) not in out and all(v > 0 for _, v in out)
    assert [v for _, v in out] == [1170.0, 1173.29, 1187.88, 1190.0]


def test_clean_nav_series_keeps_real_step_changes():
    from datetime import date
    from ingestion.jobs.mufap_fund_navs import clean_nav_series
    # a permanent level shift (e.g. a unit split) is not an isolated spike
    pts = [(date(2024, 1, i), v) for i, v in [(1, 100.0), (2, 101.0), (3, 50.0), (4, 50.5), (5, 51.0)]]
    assert len(clean_nav_series(pts)) == 5
