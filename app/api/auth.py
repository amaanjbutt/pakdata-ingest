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

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fastapi import Depends

from app import db
from app.security import (Caller, authenticate_session_or_key, create_session,
                          delete_session, generate_key, get_redis, hash_password,
                          verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])

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
