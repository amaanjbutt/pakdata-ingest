from __future__ import annotations

from datetime import date

from app.api.series import _csv_response, _csv_safe


def test_csv_safe_prefixes_dangerous_leads():
    for bad in ("=cmd()", "+1", "-1+2", "@SUM(A1)", "\ttab", "\rcr"):
        assert _csv_safe(bad).startswith("'")


def test_csv_safe_leaves_normal_values():
    assert _csv_safe("wheat") == "wheat"
    assert _csv_safe(11.02) == "11.02"
    assert _csv_safe(None) == ""


def test_csv_response_neutralizes_formula_value():
    rows = [{"obs_date": date(2026, 7, 16), "value": "=1+1", "dims": {}}]
    body = _csv_response("kibor.3m", rows).body.decode()
    # the malicious value is present but neutralized with a leading quote
    assert "'=1+1" in body
    assert ",=1+1" not in body
