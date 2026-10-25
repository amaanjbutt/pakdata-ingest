"""Admin panel API: every tab answers for an admin, and nobody else gets in."""
from __future__ import annotations

import uuid

import pytest

from tests.test_api_integration import _db_available, client  # noqa: F401 — fixture

needs_db = pytest.mark.skipif(not _db_available(), reason="no database")
TABS = ("overview", "users", "ingestion", "dq", "traffic", "catalog", "system")


@pytest.fixture
def session(client, monkeypatch):  # noqa: F811
    """(admin session token, non-admin session token)."""
    from app import db
    from app.config import settings
    from app.security import get_redis

    emails = [f"admin-{uuid.uuid4().hex[:10]}@pakdata.local", f"user-{uuid.uuid4().hex[:10]}@pakdata.local"]
    monkeypatch.setattr(settings, "admin_emails", emails[0])
    tokens = []
    for e in emails:
        get_redis().delete(*[k for k in get_redis().scan_iter("signup:ip:*")] or ["_"])
        r = client.post("/auth/signup", json={"email": e, "password": "hunter2pass"})
        assert r.status_code == 200, r.text
        tokens.append(r.json()["session_token"])
    yield tokens
    for e in emails:
        row = db.query_one("SELECT id FROM users WHERE email = %s", (e,))
        if row:
            for t in ("sessions", "api_keys", "usage_daily"):
                db.execute(f"DELETE FROM {t} WHERE user_id = %s", (row["id"],))
            db.execute("DELETE FROM users WHERE id = %s", (row["id"],))


@needs_db
def test_admin_tabs_answer_for_admins_only(client, session):  # noqa: F811
    admin, user = session
    for tab in TABS:
        r = client.get(f"/v1/admin/{tab}", headers={"X-Session-Token": admin})
        assert r.status_code == 200, (tab, r.text)
        assert r.json()["success"] is True
        assert client.get(f"/v1/admin/{tab}", headers={"X-Session-Token": user}).status_code == 403
        assert client.get(f"/v1/admin/{tab}").status_code == 401


@needs_db
def test_ingestion_tab_lists_every_registered_job(client, session):  # noqa: F811
    from ingestion.registry import JOBS

    body = client.get("/v1/admin/ingestion", headers={"X-Session-Token": session[0]}).json()
    names = {j["job"] for j in body["jobs"]}
    assert set(JOBS) <= names
    where = {j["job"]: j["where"] for j in body["jobs"]}
    assert where.get("easydata_sync") == "github" and where.get("amis_prices") == "vps"
    assert where.get("pbs_trade_hs") == "workstation"
