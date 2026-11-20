from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app import security
from app.security import (
    PAST_DUE_GRACE_DAYS,
    PLAN_LIMITS,
    Caller,
    _subscription_ok,
    authenticate,
    enforce_tier,
    generate_key,
    hash_key,
    history_floor,
)


def _caller(plan: str) -> Caller:
    return Caller(user_id="u1", plan=plan, limits=PLAN_LIMITS[plan])


class _Req:
    """Minimal stand-in for a Starlette Request (only .state is used)."""

    class _State:
        pass

    def __init__(self):
        self.state = _Req._State()


def test_null_subscription_status_is_rejected(monkeypatch):
    # A user with a real plan but NULL subscription_status must NOT get access.
    monkeypatch.setattr(
        security.db,
        "query_one",
        lambda *a, **k: {"user_id": "u1", "plan": "pro", "subscription_status": None},
    )
    with pytest.raises(HTTPException) as ei:
        authenticate(_Req(), x_api_key="pk_live_whatever", api_key=None)
    assert ei.value.status_code == 402


def test_active_subscription_is_accepted(monkeypatch):
    monkeypatch.setattr(
        security.db,
        "query_one",
        lambda *a, **k: {"user_id": "u1", "plan": "developer", "subscription_status": "active"},
    )
    # Rate-limit path touches Redis; stub it so the unit test needs no server.
    monkeypatch.setattr(security, "_enforce_rate_limit", lambda request, caller: None)
    caller = authenticate(_Req(), x_api_key="pk_live_whatever", api_key=None)
    assert caller.plan == "developer"


def test_legacy_basic_plan_resolves_to_developer(monkeypatch):
    # Rows stored under the old two-tier name keep working via the alias map.
    monkeypatch.setattr(
        security.db,
        "query_one",
        lambda *a, **k: {"user_id": "u1", "plan": "basic", "subscription_status": "active"},
    )
    monkeypatch.setattr(security, "_enforce_rate_limit", lambda request, caller: None)
    caller = authenticate(_Req(), x_api_key="pk_live_whatever", api_key=None)
    assert caller.plan == "developer"
    assert caller.limits is PLAN_LIMITS["developer"]


def test_active_subscription_ok_regardless_of_period_end():
    assert _subscription_ok("active", None) is True


def test_past_due_within_grace_is_allowed():
    end = date.today() - timedelta(days=PAST_DUE_GRACE_DAYS - 1)
    assert _subscription_ok("past_due", end) is True


def test_past_due_past_grace_is_blocked():
    end = date.today() - timedelta(days=PAST_DUE_GRACE_DAYS + 1)
    assert _subscription_ok("past_due", end) is False


def test_past_due_without_period_end_is_blocked():
    assert _subscription_ok("past_due", None) is False


def test_null_and_canceled_status_blocked():
    assert _subscription_ok(None, None) is False
    assert _subscription_ok("canceled", date.today()) is False


def test_past_due_grace_accepts_datetime_period_end():
    from datetime import datetime, timezone
    end = datetime.now(timezone.utc)  # today, as a timestamptz
    assert _subscription_ok("past_due", end) is True


def test_generated_key_is_prefixed_and_hashes_stably():
    plaintext, h = generate_key()
    assert plaintext.startswith("pk_live_")
    assert h == hash_key(plaintext)
    assert len(h) == 64  # sha256 hex


def test_hash_is_not_reversible_and_deterministic():
    assert hash_key("abc") == hash_key("abc")
    assert hash_key("abc") != hash_key("abd")


def test_free_history_floor_is_five_years():
    # 2026-11-20: the free API reaches the last 5 years (the public chart's window)
    floor = history_floor(_caller("free"))
    today = date.today()
    assert floor is not None
    assert floor.year == today.year - 5
    assert (floor.month, floor.day) in {(today.month, today.day), (2, 28)}


def test_paid_plans_have_full_history():
    for plan in ("developer", "pro", "business", "internal"):
        assert history_floor(_caller(plan)) is None


def test_points_per_call():
    from app.security import page_limit

    assert page_limit(_caller("free"), 5000) == 1000
    assert page_limit(_caller("free"), 200) == 200
    assert page_limit(_caller("developer"), 5000) == 5000
    assert page_limit(_caller("pro"), 10000) == 5000
    assert page_limit(_caller("internal"), 10000) == 10000


def test_daily_quotas_match_the_plan_table():
    assert PLAN_LIMITS["free"].per_day == 20 and PLAN_LIMITS["free"].per_min == 10
    assert PLAN_LIMITS["free"].downloads_per_day == 5
    assert "pass" not in PLAN_LIMITS  # the Data pass was withdrawn before sale (2026-11-20)
    assert PLAN_LIMITS["developer"].downloads_per_day is None
    assert PLAN_LIMITS["developer"].per_day == 2_000
    assert PLAN_LIMITS["pro"].per_day == 20_000
    assert PLAN_LIMITS["business"].per_day == 200_000
    assert PLAN_LIMITS["free"].credits is None  # no monthly allowance any more
    assert not PLAN_LIMITS["internal"].public


def test_files_and_vintages_by_plan():
    lim = PLAN_LIMITS
    assert lim["pro"].bulk and lim["business"].bulk
    assert not lim["free"].bulk and not lim["developer"].bulk
    assert lim["pro"].vintages and lim["pro"].vintage_downloads
    assert not lim["developer"].vintages and not lim["developer"].vintage_downloads


def _row(**kw):
    base = {"plan": "free", "subscription_status": "active", "limits_version": 2, "legacy_free_until": None}
    return {**base, **kw}


def test_grandfathered_limits():
    from app.security import LEGACY_LIMITS, effective_limits

    assert effective_limits("pro", _row(plan="pro", limits_version=1))[1] is LEGACY_LIMITS["pro"]
    assert effective_limits("pro", _row(plan="pro"))[1] is PLAN_LIMITS["pro"]
    later = date.today() + timedelta(days=30)
    plan, lim = effective_limits("free", _row(legacy_free_until=later))
    assert plan == "free" and lim.credits == 500 and lim.per_day == 500
    past = date.today() - timedelta(days=1)
    assert effective_limits("free", _row(legacy_free_until=past))[1] is PLAN_LIMITS["free"]


def test_every_plan_reaches_every_dataset():
    # Since 2026-09-25 free reaches funds + alternate data too; plans differ by
    # volume, history, files, vintages and webhooks.
    for plan in ("free", "developer", "pro", "business"):
        enforce_tier(_caller(plan), {"tier": "pro"})  # no raise


def test_free_allowed_core_series():
    enforce_tier(_caller("free"), {"tier": "basic"})  # no raise
