"""API-key auth, rate limiting, and tier gating (PRD §7).

- Keys are presented in `X-API-Key` (or `?api_key=` for quick testing) and stored
  only as sha256 hashes (PRD §10).
- Rate limiting is a Redis sliding window keyed by user id, per-minute and per-day.
- Tier gating: `series.tier = 'pro'` (funds + alternate data) requires a paid
  plan; history-depth gating clamps Free to 2 years and Developer to 10 years.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import redis
from fastapi import Header, HTTPException, Query, Request

from app import db
from app.config import settings

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# ---- key handling -----------------------------------------------------------

KEY_PREFIX = "pk_live_"


def generate_key() -> tuple[str, str]:
    """Return (plaintext, sha256_hash). Plaintext is shown to the user once."""
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_key(plaintext)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


# ---- password hashing (pbkdf2, stdlib — no external dep) --------------------

import base64  # noqa: E402

_PBKDF2_ITER = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITER)
    return (
        f"pbkdf2_sha256${_PBKDF2_ITER}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _algo, iters, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return secrets.compare_digest(dk, expected)
    except Exception:
        return False


# ---- sessions (dashboard login) --------------------------------------------

SESSION_TTL_DAYS = 30


def create_session(user_id: str) -> str:
    """Create a session, return the plaintext token (stored only as a hash)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(tz=timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        (hash_key(token), user_id, expires),
    )
    return token


def resolve_session(token: str | None) -> str | None:
    """Return the user_id for a live session token, or None."""
    if not token:
        return None
    row = db.query_one(
        "SELECT user_id FROM sessions WHERE token_hash = %s AND expires_at > now()",
        (hash_key(token),),
    )
    return str(row["user_id"]) if row else None


def delete_session(token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_key(token),))


# ---- plan limits ------------------------------------------------------------

@dataclass(frozen=True)
class PlanLimits:
    per_min: int
    per_day: int
    history_years: int | None  # None = full history
    allow_pro: bool


# Four public tiers (PRODUCT.md §7). `allow_pro` = access to tier='pro' series
# (funds + alternate data); only Free is excluded. history_years=None = full
# history to 1947.
PLAN_LIMITS = {
    "free": PlanLimits(per_min=10, per_day=100, history_years=1, allow_pro=False),
    "developer": PlanLimits(per_min=60, per_day=10_000, history_years=10, allow_pro=True),
    "pro": PlanLimits(per_min=300, per_day=100_000, history_years=None, allow_pro=True),
    "business": PlanLimits(per_min=600, per_day=500_000, history_years=None, allow_pro=True),
}

# Legacy plan names → current ones (the pre-2026-07-31 two-tier model used
# 'basic'). Kept so existing keys/rows keep working without a hard migration.
_PLAN_ALIASES = {"basic": "developer"}


def resolve_plan(plan: str | None) -> str | None:
    """Normalize a stored plan name to a current tier, or None if unknown."""
    if plan is None:
        return None
    plan = _PLAN_ALIASES.get(plan, plan)
    return plan if plan in PLAN_LIMITS else None


@dataclass
class Caller:
    user_id: str
    plan: str
    limits: PlanLimits


# A failed renewal shouldn't cut a paying customer off instantly. LemonSqueezy
# retries the charge over a few days; we keep access alive until `period_end`
# (their paid-through date) plus this grace, giving the retry time to succeed
# before the key stops working.
PAST_DUE_GRACE_DAYS = 3


def _subscription_ok(status: str | None, period_end: datetime | date | None) -> bool:
    """True if this subscription may access data: active, or past_due but still
    within the grace window past its paid-through date."""
    if status == "active":
        return True
    if status == "past_due" and period_end is not None:
        end = period_end.date() if isinstance(period_end, datetime) else period_end
        return date.today() <= end + timedelta(days=PAST_DUE_GRACE_DAYS)
    return False


# ---- auth dependency --------------------------------------------------------

def _resolve_key(x_api_key: str | None, api_key: str | None) -> str:
    key = x_api_key or api_key
    if not key:
        raise HTTPException(status_code=401, detail="missing API key")
    return key


def authenticate(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> Caller:
    key = _resolve_key(x_api_key, api_key)
    row = db.query_one(
        """
        SELECT u.id AS user_id, u.plan, u.subscription_status, u.period_end
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = %(h)s AND k.revoked_at IS NULL
        """,
        {"h": hash_key(key)},
    )
    if not row:
        raise HTTPException(status_code=401, detail="invalid API key")
    plan = resolve_plan(row["plan"])
    if plan is None:
        raise HTTPException(status_code=402, detail="no active subscription")
    # Only 'active' — or 'past_due' still inside the grace window — passes. A
    # NULL/missing status must NOT grant access: a signup flow that inserts a user
    # before payment could otherwise land with a real plan but NULL status and
    # read for free.
    if not _subscription_ok(row["subscription_status"], row.get("period_end")):
        if row["subscription_status"] == "past_due":
            raise HTTPException(status_code=402, detail="subscription past due")
        raise HTTPException(status_code=402, detail="subscription inactive")

    caller = Caller(user_id=str(row["user_id"]), plan=plan, limits=PLAN_LIMITS[plan])
    _enforce_rate_limit(request, caller)
    return caller


def authenticate_session_or_key(
    request: Request,
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> Caller:
    """Auth for dashboard/account endpoints: accept a session token (from login)
    OR an API key. Sessions aren't rate-limited (interactive dashboard use)."""
    if x_session_token:
        user_id = resolve_session(x_session_token)
        if not user_id:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        row = db.query_one(
            "SELECT plan, subscription_status, period_end FROM users WHERE id = %(id)s",
            {"id": user_id},
        )
        if not row:
            raise HTTPException(status_code=401, detail="invalid session")
        plan = resolve_plan(row["plan"]) or "free"
        return Caller(user_id=user_id, plan=plan, limits=PLAN_LIMITS[plan])
    # Fall back to API-key auth (rate-limited).
    return authenticate(request, x_api_key, api_key)


# ---- rate limiting ----------------------------------------------------------

def _enforce_rate_limit(request: Request, caller: Caller) -> None:
    r = get_redis()
    now = time.time()
    # True sliding 60s window: a sorted set of request timestamps, trimmed to the
    # last 60s, so a caller can't burst ~2x by straddling a fixed minute boundary.
    minute_key = f"rl:min:{caller.user_id}"
    member = f"{now}-{secrets.token_hex(6)}"
    # Daily budget stays a cheap fixed-window counter (a sliding day is costly).
    day_key = f"rl:day:{caller.user_id}:{date.today().isoformat()}"

    pipe = r.pipeline()
    pipe.zremrangebyscore(minute_key, 0, now - 60)
    pipe.zadd(minute_key, {member: now})
    pipe.zcard(minute_key)
    pipe.expire(minute_key, 120)
    pipe.incr(day_key)
    pipe.expire(day_key, 172800)
    _, _, minute_count, _, day_count, _ = pipe.execute()

    remaining_min = max(caller.limits.per_min - int(minute_count), 0)
    remaining_day = max(caller.limits.per_day - int(day_count), 0)
    # Surface limits to the client (headers attached by middleware via request.state).
    request.state.ratelimit_remaining = min(remaining_min, remaining_day)
    request.state.ratelimit_limit = caller.limits.per_min

    if int(minute_count) > caller.limits.per_min:
        # Sliding window: the oldest request in the window leaves in <= 60s.
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded (per-minute)",
            headers={"Retry-After": "60"},
        )
    if int(day_count) > caller.limits.per_day:
        raise HTTPException(
            status_code=429,
            detail="daily quota exceeded",
            headers={"Retry-After": "3600"},
        )


# ---- tier & history gating --------------------------------------------------

def enforce_tier(caller: Caller, series_row: dict) -> None:
    if series_row.get("tier") == "pro" and not caller.limits.allow_pro:
        raise HTTPException(status_code=403, detail="series requires Pro tier")


def history_floor(caller: Caller) -> date | None:
    """Earliest date a caller may access, or None for full history.

    Subtracts exact calendar years (no 365-day drift) using stdlib only; Feb 29
    is clamped to Feb 28 in non-leap target years."""
    yrs = caller.limits.history_years
    if yrs is None:
        return None
    today = date.today()
    try:
        return today.replace(year=today.year - yrs)
    except ValueError:  # Feb 29 -> Feb 28
        return today.replace(year=today.year - yrs, day=28)
