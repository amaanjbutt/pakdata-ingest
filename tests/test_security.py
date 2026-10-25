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


def test_developer_history_floor_is_about_ten_years():
    floor = history_floor(_caller("developer"))
    assert floor is not None
    assert floor < date.today() - timedelta(days=365 * 10 - 2)


def test_free_history_floor_is_about_one_year():
    floor = history_floor(_caller("free"))
    assert floor is not None
    assert floor.year == date.today().year - 1


def test_pro_has_full_history():
    assert history_floor(_caller("pro")) is None


def test_business_has_full_history():
    assert history_floor(_caller("business")) is None


def test_developer_history_floor_has_no_365day_drift():
    # Exact calendar-year subtraction: same month/day (or Feb 28 clamp), never
    # ~5 days early like 365*10.
    floor = history_floor(_caller("developer"))
    today = date.today()
    assert floor is not None
    assert floor.year == today.year - 10
    assert (floor.month, floor.day) in {(today.month, today.day), (2, 28)}


def test_every_plan_reaches_every_dataset():
    # Since 2026-09-25 free reaches funds + alternate data too (1-year window,
    # one-time credits); plans differ by volume/history/vintages/webhooks.
    for plan in ("free", "developer", "pro", "business"):
        enforce_tier(_caller(plan), {"tier": "pro"})  # no raise


def test_free_allowed_core_series():
    enforce_tier(_caller("free"), {"tier": "basic"})  # no raise
