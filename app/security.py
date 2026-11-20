"""API-key auth, rate limiting, and tier gating (PRD §7).

- Keys are presented in `X-API-Key` (or `?api_key=` for quick testing) and stored
  only as sha256 hashes (PRD §10).
- Rate limiting is a Redis sliding window keyed by user id, per-minute and per-day.
- Plans (PLAN_LIMITS): calls per day and minute, history depth (free = last 5 years), points per
  call, site downloads per day, full-history charts per day, whole-dataset files, vintages.
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

# Plans for developers and researchers (owner, 2026-11-13; docs/decisions.md 2026-11-20).
# A free account is the hook: 20 API calls a day (1 call = 1 request) over the last 5 years at
# up to 1,000 points per call, and 5 site downloads a day (one series, full history). Developer /
# Pro / Business are subscriptions (a month of Developer, cancelled, covers a one-off project);
# whole-dataset files and vintages are Pro and Business. Every plan reaches every dataset. Day
# counters reset at 00:00 UTC. (A one-time $9 "Data pass" was withdrawn before sale, 2026-11-20.)
FREE_CALLS_PER_DAY = 20
FREE_DOWNLOADS_PER_DAY = 5
FREE_HISTORY_YEARS = 5

# The old free allowance (500 calls a month, 2026-10-04 → 2026-11-20). Only grandfathered free
# keys (users.legacy_free_until) still use it; see LEGACY_LIMITS.
FREE_CREDITS = 500


@dataclass(frozen=True)
class PlanLimits:
    per_min: int
    per_day: int                       # API calls per day
    history_years: int | None          # None = full history
    allow_pro: bool = True             # every tier reaches every dataset (kept for compatibility)
    max_page: int = 5000               # most points one call returns (more = more pages = more calls)
    downloads_per_day: int | None = None   # site downloads; None = unlimited
    charts_per_day: int = 500          # full-history charts on the website, series per day
    bulk: bool = False                 # whole-dataset files (GET /v1/bulk)
    vintages: bool = False             # ?vintage= and /revisions through the API
    vintage_downloads: bool = False    # a series as of a past date, as a file
    credits: int | None = None         # legacy monthly allowance (grandfathered free keys only)
    public: bool = True                # listed in /v1/plans


PLAN_LIMITS: dict[str, PlanLimits] = {
    "free": PlanLimits(per_min=10, per_day=FREE_CALLS_PER_DAY, history_years=FREE_HISTORY_YEARS,
                       max_page=1000, downloads_per_day=FREE_DOWNLOADS_PER_DAY, charts_per_day=50),
    "developer": PlanLimits(per_min=60, per_day=2_000, history_years=None),
    "pro": PlanLimits(per_min=300, per_day=20_000, history_years=None, charts_per_day=1000,
                      bulk=True, vintages=True, vintage_downloads=True),
    "business": PlanLimits(per_min=600, per_day=200_000, history_years=None, charts_per_day=2000,
                           bulk=True, vintages=True, vintage_downloads=True),
    # The website's own key (server-side rendering of every page). Not for sale, not listed.
    "internal": PlanLimits(per_min=5_000, per_day=2_000_000, history_years=None, max_page=10_000,
                           charts_per_day=100_000, bulk=True, vintages=True, vintage_downloads=True,
                           public=False),
}

# Limits sold before 2026-11-20. Paid subscribers keep them until they change plan
# (users.limits_version = 1); free keys that used more than 20 calls a day keep the old monthly
# allowance for 60 days (users.legacy_free_until). Where the new plan is more generous (full
# history on Developer, 5 years on free) they get the new terms.
LEGACY_LIMITS: dict[str, PlanLimits] = {
    "free": PlanLimits(per_min=10, per_day=FREE_CREDITS, history_years=FREE_HISTORY_YEARS,
                       max_page=5000, downloads_per_day=FREE_DOWNLOADS_PER_DAY, charts_per_day=50,
                       credits=FREE_CREDITS, public=False),
    "developer": PlanLimits(per_min=60, per_day=10_000, history_years=None, max_page=10_000,
                            public=False),
    "pro": PlanLimits(per_min=300, per_day=100_000, history_years=None, max_page=10_000,
                      charts_per_day=1000, bulk=True, vintages=True, vintage_downloads=True, public=False),
    "business": PlanLimits(per_min=600, per_day=500_000, history_years=None, max_page=10_000,
                           charts_per_day=2000, bulk=True, vintages=True, vintage_downloads=True,
                           public=False),
}

# Plans the API sells, in display order.
PUBLIC_PLANS = tuple(name for name, lim in PLAN_LIMITS.items() if lim.public)

# Legacy plan names → current ones (the pre-2026-07-31 two-tier model used
# 'basic'). Kept so existing keys/rows keep working without a hard migration.
_PLAN_ALIASES = {"basic": "developer"}

# Stored plan names (users.plan).
_STORED_PLANS = ("free", "developer", "pro", "business", "internal")


def resolve_plan(plan: str | None) -> str | None:
    """Normalize a stored plan name to a current tier, or None if unknown."""
    if plan is None:
        return None
    plan = _PLAN_ALIASES.get(plan, plan)
    return plan if plan in _STORED_PLANS else None


def effective_limits(plan: str, row: dict) -> tuple[str, PlanLimits]:
    """The plan a stored user row acts as, and its limits.

    - limits_version 1 (grandfathered subscribers) keeps the pre-2026-11-20 limits.
    - A free key with legacy_free_until in the future keeps the old monthly allowance."""
    if plan == "free":
        until = row.get("legacy_free_until")
        if until is not None and until >= date.today():
            return "free", LEGACY_LIMITS["free"]
        return "free", PLAN_LIMITS["free"]
    limits = PLAN_LIMITS[plan]
    if row.get("limits_version") == 1 and plan in LEGACY_LIMITS:
        limits = LEGACY_LIMITS[plan]
    return plan, limits


@dataclass
class Caller:
    user_id: str
    plan: str
    limits: PlanLimits
    auth: str = "key"


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


_USER_COLS = "u.plan, u.subscription_status, u.period_end, u.limits_version, u.legacy_free_until"


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
        f"""
        SELECT u.id AS user_id, k.id AS key_id, {_USER_COLS}
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = %(h)s AND k.revoked_at IS NULL
        """,
        {"h": hash_key(key)},
    )
    if not row:
        raise HTTPException(status_code=401, detail="invalid API key")
    # who is calling, for the request log (app/services/reqlog) — set before any 402/429
    request.state.user_id = str(row["user_id"])
    request.state.key_id = str(row["key_id"]) if row.get("key_id") else ""
    request.state.auth = "key"
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

    plan, limits = effective_limits(plan, row)
    caller = Caller(user_id=str(row["user_id"]), plan=plan, limits=limits)
    request.state.caller = caller
    _enforce_rate_limit(request, caller)
    # Grandfathered free keys keep the old monthly allowance: a rate-limit rejection (429)
    # above never reaches here, so a throttled request costs no credit.
    _consume_credit(caller)
    _touch_key_last_used(key)
    return caller


def _consume_credit(caller: Caller) -> None:
    """Consume one request credit for credit-metered (grandfathered free) callers. Atomic — the
    UPDATE only succeeds while a credit remains, so concurrent requests can't drive
    the balance negative. The first request in a new calendar month refills the
    allowance in the same statement. Callers without `credits` are skipped.
    Raises 402 `credits_exhausted` when this month's allowance is spent."""
    if caller.limits.credits is None:
        return
    row = db.query_one(
        """
        UPDATE users SET
            credits_remaining = CASE WHEN credits_period < date_trunc('month', now())::date
                                     THEN %(allot)s - 1 ELSE credits_remaining - 1 END,
            credits_period = date_trunc('month', now())::date
        WHERE id = %(id)s
          AND (credits_period < date_trunc('month', now())::date OR credits_remaining > 0)
        RETURNING credits_remaining
        """,
        {"id": caller.user_id, "allot": caller.limits.credits},
    )
    if row is None:
        exc = HTTPException(
            status_code=402,
            detail="this month's 500 calls are used up — they reset on the 1st, or upgrade at pakdatahub.com/pricing",
        )
        exc.error_code = "credits_exhausted"  # surfaced by the envelope handler
        raise exc


def _touch_key_last_used(key: str) -> None:
    """Record when a key was last used, throttled to ~1 write/min/key so the hot
    auth path isn't a write per request. Best-effort — never breaks auth."""
    try:
        db.execute(
            "UPDATE api_keys SET last_used_at = now() "
            "WHERE key_hash = %(h)s "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')",
            {"h": hash_key(key)},
        )
    except Exception:  # noqa: BLE001 — telemetry must never fail a request
        pass


def session_caller(request: Request, token: str | None) -> Caller:
    """The signed-in user behind a dashboard/site session token (401 if none)."""
    user_id = resolve_session(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    row = db.query_one(f"SELECT {_USER_COLS} FROM users u WHERE u.id = %(id)s", {"id": user_id})
    if not row:
        raise HTTPException(status_code=401, detail="invalid session")
    plan, limits = effective_limits(resolve_plan(row["plan"]) or "free", row)
    request.state.user_id = str(user_id)
    request.state.auth = "session"
    caller = Caller(user_id=user_id, plan=plan, limits=limits, auth="session")
    request.state.caller = caller
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
        return session_caller(request, x_session_token)
    # Fall back to API-key auth (rate-limited).
    return authenticate(request, x_api_key, api_key)


def authenticate_session(
    request: Request,
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
) -> Caller:
    """Session-only auth: website features for a signed-in person (full-history charts,
    downloads). An API key is not accepted, so a script holding a key can't use them."""
    if not x_session_token:
        raise HTTPException(status_code=401, detail="sign in to use this")
    return session_caller(request, x_session_token)


# ---- the website's own key + the visitor's IP ---------------------------------

_SITE_KEY_CACHE: dict[str, tuple[bool, float]] = {}


def is_site_key(key: str | None) -> bool:
    """True when `key` belongs to the website's own (internal-plan) account. Cached 10 min."""
    if not key:
        return False
    h = hash_key(key)
    hit = _SITE_KEY_CACHE.get(h)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        row = db.query_one(
            "SELECT 1 AS ok FROM api_keys k JOIN users u ON u.id = k.user_id "
            "WHERE k.key_hash = %s AND k.revoked_at IS NULL AND u.plan = 'internal'", (h,))
        ok = bool(row)
    except Exception:  # noqa: BLE001
        ok = False
    _SITE_KEY_CACHE[h] = (ok, time.time() + 600)
    return ok


def visitor_ip(request: Request) -> str:
    """The visitor's IP. The website forwards it (X-PakData-Client-IP) on calls it makes for a
    signed-in person; it is believed only when the site's own key comes with it. Otherwise the
    first X-Forwarded-For hop (Caddy), else the socket peer."""
    fwd = request.headers.get("x-pakdata-client-ip")
    if fwd and is_site_key(request.headers.get("x-pakdata-site-key")):
        return fwd.strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---- rate limiting ----------------------------------------------------------

def _enforce_rate_limit(request: Request, caller: Caller) -> None:
    r = get_redis()
    now = time.time()
    # True sliding 60s window: a sorted set of request timestamps, trimmed to the
    # last 60s, so a caller can't burst ~2x by straddling a fixed minute boundary.
    minute_key = f"rl:min:{caller.user_id}"
    member = f"{now}-{secrets.token_hex(6)}"
    # Daily budget stays a cheap fixed-window counter (a sliding day is costly).
    day_key = f"rl:day:{caller.user_id}:{_utc_day()}"

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
    request.state.ratelimit_day_limit = caller.limits.per_day
    request.state.ratelimit_day_remaining = remaining_day

    if int(minute_count) > caller.limits.per_min:
        # Sliding window: the oldest request in the window leaves in <= 60s.
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded (per-minute)",
            headers={"Retry-After": "60"},
        )
    if int(day_count) > caller.limits.per_day:
        exc = HTTPException(
            status_code=429,
            detail=(f"daily limit of {caller.limits.per_day:,} calls reached — it resets at 00:00 UTC"
                    + (", or upgrade at pakdatahub.com/pricing" if caller.plan in ("free", "developer")
                       else "")),
            headers={"Retry-After": str(_seconds_to_utc_midnight())},
        )
        exc.error_code = "daily_limit"
        raise exc


def _utc_day() -> str:
    return datetime.now(tz=timezone.utc).date().isoformat()


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(tz=timezone.utc)
    nxt = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return max(int((nxt - now).total_seconds()), 1)


def calls_used_today(user_id: str) -> int:
    return int(get_redis().get(f"rl:day:{user_id}:{_utc_day()}") or 0)


# ---- site downloads + full-history charts (per-day meters) ---------------------

def downloads_used_today(user_id: str) -> int:
    return int(get_redis().get(f"dl:day:{user_id}:{_utc_day()}") or 0)


def take_download(caller: Caller) -> dict:
    """Count one site download against the caller's day; 429 `download_limit` when the free
    plan's 5 are spent. Returns {used, limit, remaining} (limit/remaining None = unlimited)."""
    r = get_redis()
    key = f"dl:day:{caller.user_id}:{_utc_day()}"
    used = int(r.incr(key))
    r.expire(key, 172800)
    cap = caller.limits.downloads_per_day
    if cap is not None and used > cap:
        r.decr(key)
        exc = HTTPException(
            status_code=429,
            detail=(f"today's {cap} free downloads are used — they reset at 00:00 UTC; every paid plan "
                    "(from $19 a month, cancel any time) downloads without a limit"),
            headers={"Retry-After": str(_seconds_to_utc_midnight())},
        )
        exc.error_code = "download_limit"
        raise exc
    return {"used": used, "limit": cap, "remaining": None if cap is None else max(cap - used, 0)}


def download_quota(caller: Caller) -> dict:
    used = downloads_used_today(caller.user_id)
    cap = caller.limits.downloads_per_day
    return {"used": used, "limit": cap, "remaining": None if cap is None else max(cap - used, 0)}


# Per-IP ceiling on full-history charts, across every account behind that address (stops one
# person cycling through free accounts). Generous: a university lab shares an address.
CHART_IP_SERIES_PER_DAY = 300


def take_chart(caller: Caller, subject: str, ip: str) -> bool:
    """Meter one full-history chart (a distinct series per account per day). Re-opening the same
    series is free. False when the account's or the address's day is spent — the page then keeps
    its 5-year chart."""
    from app.services.reqlog import ip_hash

    r = get_redis()
    day = _utc_day()
    ukey = f"ch:day:{caller.user_id}:{day}"
    ikey = f"ch:ip:{ip_hash(ip)}:{day}"
    if r.sismember(ukey, subject):
        return True
    if int(r.scard(ukey)) >= caller.limits.charts_per_day:
        return False
    if caller.plan == "free" and not r.sismember(ikey, subject) \
            and int(r.scard(ikey)) >= CHART_IP_SERIES_PER_DAY:
        return False
    pipe = r.pipeline()
    pipe.sadd(ukey, subject)
    pipe.expire(ukey, 172800)
    pipe.sadd(ikey, subject)
    pipe.expire(ikey, 172800)
    pipe.execute()
    return True


def charts_used_today(user_id: str) -> int:
    return int(get_redis().scard(f"ch:day:{user_id}:{_utc_day()}") or 0)


# ---- tier & history gating --------------------------------------------------

def enforce_tier(caller: Caller, series_row: dict) -> None:
    if series_row.get("tier") == "pro" and not caller.limits.allow_pro:
        raise HTTPException(status_code=403, detail="series requires Pro tier")


def require_vintages(caller: Caller, what: str = "revision vintages") -> None:
    """?vintage= and /revisions through the API: Pro and Business."""
    if not caller.limits.vintages:
        raise HTTPException(status_code=403, detail=f"{what} through the API: Pro or Business plan "
                                                    "(they also download a series as of a past date)")


def page_limit(caller: Caller, limit: int) -> int:
    """Clamp a requested page size to the plan's points per call (1,000 free, 5,000 paid).
    Longer histories take more pages, and each page is one call."""
    # The cap is also sent as the X-Page-Max response header (main.py middleware).
    return min(limit, caller.limits.max_page)


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
