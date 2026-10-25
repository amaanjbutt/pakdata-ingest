"""Monthly SBP bulletin refresh — issue selection and the mis-filed-upload guard."""
from __future__ import annotations

from ingestion.jobs.sbp_msb_vintages import issue_lag_ok, new_issues


def test_new_issues_only_after_the_newest_stored_and_capped():
    have = {"2026-07", "2026-08", "2026-09"}
    listed = ["2005-02", "2026-08", "2026-09", "2026-10", "2026-11", "2026-12", "2027-01"]
    assert new_issues(listed, have) == ["2026-10", "2026-11", "2026-12"]   # old gaps ignored, max 3
    assert new_issues(["2026-09"], have) == []


def test_issue_lag_guard():
    ok = {"issue": "2026-10", "rows": [{"label": "x", "values": {"2026-08-31": [1, 1], "2026-09-30": [1, 1]}}]}
    misfiled = {"issue": "2026-10", "rows": [{"label": "x", "values": {"2025-09-30": [1, 1]}}]}
    assert issue_lag_ok(ok) and not issue_lag_ok(misfiled)
