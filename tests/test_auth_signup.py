"""Email+password signup, login, logout, and session auth.

DB-backed; auto-skips offline. Run in the stack:
    docker compose exec api python -m pytest tests/test_auth_signup.py
"""
from __future__ import annotations

import uuid

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


def _db_available() -> bool:
    try:
        from app import db

        db.query_one("SELECT 1 AS ok")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="no database reachable")


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return fastapi_testclient.TestClient(app)


def _email() -> str:
    return f"signup-{uuid.uuid4().hex[:12]}@pakdata.local"


def _cleanup(email: str) -> None:
    from app import db

    row = db.query_one("SELECT id FROM users WHERE email=%s", (email,))
    if row:
        db.execute("DELETE FROM sessions WHERE user_id=%s", (row["id"],))
        db.execute("DELETE FROM api_keys WHERE user_id=%s", (row["id"],))
        db.execute("DELETE FROM users WHERE id=%s", (row["id"],))


def test_signup_returns_session_and_key(client):
    email = _email()
    try:
        r = client.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
        assert r.status_code == 200, r.text
        b = r.json()
        assert b["plan"] == "free"
        assert b["api_key"].startswith("pk_live_")
        assert b["session_token"]

        # Session authenticates the dashboard endpoints.
        acc = client.get("/v1/account", headers={"X-Session-Token": b["session_token"]})
        assert acc.status_code == 200, acc.text
        assert acc.json()["email"] == email

        # The issued API key also still works.
        u = client.get("/v1/usage", headers={"X-API-Key": b["api_key"]})
        assert u.status_code == 200
    finally:
        _cleanup(email)


def test_signup_requires_password_min_len(client):
    r = client.post("/auth/signup", json={"email": _email(), "password": "short"})
    assert r.status_code == 422


def test_signup_rejects_bad_email(client):
    r = client.post("/auth/signup", json={"email": "nope", "password": "hunter2pass"})
    assert r.status_code == 422


def test_login_and_logout(client):
    email = _email()
    try:
        client.post("/auth/signup", json={"email": email, "password": "hunter2pass"})
        # wrong password
        bad = client.post("/auth/login", json={"email": email, "password": "wrongpass1"})
        assert bad.status_code == 401
        # right password
        ok = client.post("/auth/login", json={"email": email, "password": "hunter2pass"})
        assert ok.status_code == 200
        token = ok.json()["session_token"]
        assert client.get("/v1/account", headers={"X-Session-Token": token}).status_code == 200
        # logout invalidates the session
        client.post("/auth/logout", json={"session_token": token})
        assert client.get("/v1/account", headers={"X-Session-Token": token}).status_code == 401
    finally:
        _cleanup(email)


def test_duplicate_email_conflicts(client):
    email = _email()
    try:
        assert client.post("/auth/signup", json={"email": email, "password": "hunter2pass"}).status_code == 200
        assert client.post("/auth/signup", json={"email": email, "password": "hunter2pass"}).status_code == 409
    finally:
        _cleanup(email)
