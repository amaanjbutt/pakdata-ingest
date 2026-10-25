"""Self-serve auth: email+password signup, login, logout (session-based).

The dashboard authenticates with a server-side **session** (an httpOnly cookie
set by the frontend from the token returned here), not by pasting an API key.
Signup also issues one free API key immediately so a new user has something to
call the API with.

- Passwords are pbkdf2-hashed (see security.hash_password), never stored plain.
- Free accounts are created `active` so their key works right away.
- One account per email; per-IP throttle so the free tier can't be scripted.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import urllib.parse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from fastapi import Depends

from app import db
from app.config import settings
from app.security import (Caller, authenticate_session_or_key, create_session,
                          delete_session, generate_key, get_redis, hash_password,
                          verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LEN = 8
SIGNUPS_PER_IP_PER_DAY = 20


class SignupRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_signup_ip_limit(request: Request) -> None:
    from datetime import date

    key = f"signup:ip:{_client_ip(request)}:{date.today().isoformat()}"
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 172800)
    count, _ = pipe.execute()
    if int(count) > SIGNUPS_PER_IP_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="too many signups from this address; try again tomorrow",
            headers={"Retry-After": "3600"},
        )


@router.post("/signup")
def signup(body: SignupRequest, request: Request):
    email = (body.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="a valid email is required")
    if len(body.password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"password must be at least {MIN_PASSWORD_LEN} characters",
        )

    _enforce_signup_ip_limit(request)

    if db.query_one("SELECT id FROM users WHERE email = %s", (email,)):
        raise HTTPException(
            status_code=409,
            detail="an account already exists for this email; sign in instead",
        )

    row = db.query_one(
        "INSERT INTO users (email, plan, subscription_status, password_hash) "
        "VALUES (%s, 'free', 'active', %s) RETURNING id",
        (email, hash_password(body.password)),
    )
    user_id = str(row["id"])

    # Issue one free API key immediately (shown once).
    plaintext, key_hash = generate_key()
    prefix = plaintext[:16] + "…"
    db.execute(
        "INSERT INTO api_keys (key_hash, user_id, label, key_prefix) "
        "VALUES (%s, %s, %s, %s)",
        (key_hash, user_id, "free signup", prefix),
    )

    token = create_session(user_id)
    # Also email the key so it isn't lost (best-effort; on-screen copy remains).
    from app.services.email import send_api_key_email, send_admin_notification
    send_api_key_email(email, plaintext, plan="free")
    send_admin_notification("New signup", f"<p>New free signup: <b>{email}</b></p>")
    return {
        "success": True,
        "session_token": token,
        "email": email,
        "plan": "free",
        "api_key": plaintext,
        "note": "store this key now — it is shown only once",
    }


@router.post("/login")
def login(body: LoginRequest, request: Request):
    email = (body.email or "").strip().lower()
    row = db.query_one(
        "SELECT id, password_hash FROM users WHERE email = %s", (email,)
    )
    # Constant-ish behaviour: same error whether email or password is wrong.
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid email or password")

    token = create_session(str(row["id"]))
    return {"success": True, "session_token": token, "email": email}


class LogoutRequest(BaseModel):
    session_token: str | None = None


@router.post("/logout")
def logout(body: LogoutRequest):
    delete_session(body.session_token)
    return {"success": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    caller: Caller = Depends(authenticate_session_or_key),
):
    if len(body.new_password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"new password must be at least {MIN_PASSWORD_LEN} characters",
        )
    row = db.query_one(
        "SELECT password_hash FROM users WHERE id = %s", (caller.user_id,)
    )
    if not row or not verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    db.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (hash_password(body.new_password), caller.user_id),
    )
    return {"success": True}


class SetPasswordRequest(BaseModel):
    token: str
    password: str


@router.post("/set-password")
def set_password(body: SetPasswordRequest):
    """Set an initial password for a pay-first buyer, authorized by their one-time
    signup token (from the checkout success redirect, /welcome?token=). Works only
    while the account has no password yet, so the token can't be replayed to hijack
    an account that already has a login. Returns a session so they're signed in."""
    if len(body.password or "") < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"password must be at least {MIN_PASSWORD_LEN} characters",
        )
    token_hash = hashlib.sha256((body.token or "").encode()).hexdigest()
    row = db.query_one("SELECT user_id FROM key_claims WHERE token_hash = %s", (token_hash,))
    if not row:
        raise HTTPException(status_code=404, detail="invalid or expired token")
    user_id = str(row["user_id"])
    urow = db.query_one("SELECT email, password_hash FROM users WHERE id = %s", (user_id,))
    if not urow:
        raise HTTPException(status_code=404, detail="account not found")
    if urow["password_hash"]:
        raise HTTPException(
            status_code=409,
            detail="this account already has a password; sign in instead",
        )
    db.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (hash_password(body.password), user_id),
    )
    token = create_session(user_id)
    return {"success": True, "session_token": token, "email": urow["email"]}


# ---- Google sign-in (server-side OAuth authorization-code flow) --------------

_OAUTH_STATE_TTL = 600  # seconds


@router.get("/google/config")
def google_config():
    """Whether Google sign-in is available (drives the frontend button)."""
    return {"enabled": bool(settings.google_client_id and settings.google_client_secret)}


@router.get("/google/start")
def google_start():
    """Redirect the browser to Google's consent screen with a one-time state."""
    if not (settings.google_client_id and settings.google_client_secret):
        raise HTTPException(status_code=503, detail="google sign-in not configured")
    state = secrets.token_urlsafe(24)
    get_redis().setex(f"oauth:state:{state}", _OAUTH_STATE_TTL, "1")
    params = urllib.parse.urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    })
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)


def _google_error_redirect(reason: str) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.public_base_url}/login?error={urllib.parse.quote(reason)}", status_code=302
    )


@router.get("/google/callback")
def google_callback(code: str | None = None, state: str | None = None,
                    error: str | None = None):
    """Exchange the code, upsert the user by verified email, issue a session, and
    bounce back to the frontend with the token in the URL fragment."""
    if error or not code or not state:
        return _google_error_redirect("google sign-in was cancelled")
    r = get_redis()
    if not r.delete(f"oauth:state:{state}"):
        return _google_error_redirect("expired sign-in — please try again")
    try:
        tok = httpx.post(GOOGLE_TOKEN_URL, data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15)
        tok.raise_for_status()
        access_token = tok.json().get("access_token")
        info = httpx.get(GOOGLE_USERINFO_URL,
                         headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        info.raise_for_status()
        profile = info.json()
    except Exception:  # noqa: BLE001 — any provider failure => graceful bounce
        return _google_error_redirect("google sign-in failed — please try again")

    email = (profile.get("email") or "").strip().lower()
    if not email or not profile.get("email_verified"):
        return _google_error_redirect("your Google email is not verified")

    row = db.query_one("SELECT id FROM users WHERE email = %s", (email,))
    if row:
        user_id = str(row["id"])
    else:
        new = db.query_one(
            "INSERT INTO users (email, plan, subscription_status) "
            "VALUES (%s, 'free', 'active') RETURNING id",
            (email,),
        )
        user_id = str(new["id"])
        from app.services.email import send_welcome_email, send_admin_notification
        send_welcome_email(email)
        send_admin_notification("New signup (Google)", f"<p>New Google signup: <b>{email}</b></p>")

    token = create_session(user_id)
    return RedirectResponse(
        f"{settings.public_base_url}/auth/callback#session={token}", status_code=302
    )
