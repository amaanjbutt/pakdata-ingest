from __future__ import annotations

import os

import pytest

from ingestion.jobs.sbp_policy_rate import parse_policy_html

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "policy_rate_live.html")


def _load() -> str:
    with open(FIXTURE, encoding="utf-8") as f:
        return f.read()


def test_parses_current_corridor():
    rates = parse_policy_html(_load())
    assert rates["rates.policy"] == 11.50
    assert rates["rates.policy.floor"] == 10.50
    assert rates["rates.policy.ceiling"] == 12.50


def test_corridor_brackets_policy_rate():
    r = parse_policy_html(_load())
    assert r["rates.policy.floor"] < r["rates.policy"] < r["rates.policy.ceiling"]


def test_missing_rate_raises():
    with pytest.raises(ValueError):
        parse_policy_html("<html><body>no rate here</body></html>")
