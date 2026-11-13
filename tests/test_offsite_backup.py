"""Off-site backup upload (ingestion/offsite_backup.py) and its freshness check."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections import namedtuple
from datetime import date, timedelta

import pytest

import ingestion.scheduler as sched
from ingestion import offsite_backup as ob

needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")
KEY = "test-passphrase-not-a-secret"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_OFFSITE_REPO", "owner/backups")
    monkeypatch.setenv("BACKUP_OFFSITE_TOKEN", "t")
    monkeypatch.setenv("BACKUP_OFFSITE_KEY", KEY)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    return tmp_path


class _Store:
    def __init__(self, names=()):
        self.assets_ = [{"id": i, "name": n, "size": 1, "state": "uploaded"} for i, n in enumerate(names)]
        self.blobs: dict[str, bytes] = {}
        self.labels: dict[int, str] = {}
        self.uploads = 0

    def release(self):
        return {"id": 1, "upload_url": "https://uploads.example/x{?name,label}"}

    def assets(self, rel):
        return [dict(a) for a in self.assets_]

    def upload(self, rel, name, chunks, size):
        data = b"".join(chunks)
        assert len(data) == size, "Content-Length must match the encrypted stream"
        self.uploads += 1
        aid = 1000 + self.uploads
        self.blobs[name] = data
        self.assets_.append({"id": aid, "name": name, "size": len(data), "state": "uploaded"})
        return {"id": aid, "name": name, "size": len(data), "state": "uploaded"}

    def set_label(self, aid, label):
        self.labels[aid] = label

    def delete(self, aid):
        self.assets_ = [a for a in self.assets_ if a["id"] != aid]


class _Redis(dict):
    def set(self, k, v, nx=False, ex=None):
        if nx and k in self:
            return None
        self[k] = v
        return True

    def delete(self, k):
        return 1 if self.pop(k, None) is not None else 0

    def info(self, section=None):
        return {"used_memory": 1, "maxmemory": 0}


@needs_openssl
@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 4096, 100_003])
def test_encrypted_size_matches_openssl(n, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_OFFSITE_KEY", KEY)
    p = tmp_path / "f"
    p.write_bytes(os.urandom(n))
    out = b"".join(ob.encrypt_stream(str(p), hashlib.sha256()))
    assert len(out) == ob.encrypted_size(n)


def test_keep_rule_matches_backup_rotate():
    newest = date(2026, 11, 11)
    names = [f"pakdata_{newest - timedelta(days=i):%Y%m%d}_200000.dump.enc" for i in range(120)]
    kept = ob.keep(names + ["README.md", "pakdata_20261111_090000.dump.enc"], daily=7, weekly=4, monthly=3)
    assert kept == {f"pakdata_202611{d:02d}_200000.dump.enc" for d in range(5, 12)} | {
        "pakdata_20261101_200000.dump.enc", "pakdata_20261025_200000.dump.enc",
        "pakdata_20261031_200000.dump.enc", "pakdata_20260930_200000.dump.enc"}


def test_unconfigured_does_nothing(monkeypatch):
    monkeypatch.delenv("BACKUP_OFFSITE_REPO", raising=False)
    assert ob.run(store=_Store()) == {"status": "unconfigured"}


@needs_openssl
def test_upload_round_trip_rotation_and_marker(env):
    dump = env / "pakdata_20261113_200000.dump"
    payload = os.urandom(300_001)
    dump.write_bytes(payload)
    (env / "pakdata_20261112_200000.dump").write_bytes(b"older")
    (env / "last_ok").write_text(f"{int(time.time())} x {dump.name} {len(payload)}\n")
    old = [f"pakdata_{date(2026, 11, 12) - timedelta(days=i):%Y%m%d}_200000.dump.enc" for i in range(40)]
    store, redis = _Store(old), _Redis()

    out = ob.run(store=store, redis_client=redis)

    assert out["uploaded"] and out["asset"] == "pakdata_20261113_200000.dump.enc"
    blob = store.blobs[out["asset"]]
    assert blob != payload and blob.startswith(b"Salted__")
    dec = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256",
                          "-pass", f"pass:{KEY}"], input=blob, capture_output=True, check=True).stdout
    assert dec == payload
    assert next(iter(store.labels.values())) == f"sha256 {hashlib.sha256(blob).hexdigest()}"
    left = {a["name"] for a in store.assets_}
    assert len(left) == len(out["kept"]) < 15 and out["asset"] in left and out["removed"]
    assert redis[ob.REDIS_KEY].endswith(out["asset"])

    again = ob.run(store=store, redis_client=redis)          # already there with the right size → no upload
    assert again["uploaded"] is False and store.uploads == 1


@needs_openssl
def test_wrong_size_asset_is_replaced(env):
    dump = env / "pakdata_20261113_200000.dump"
    dump.write_bytes(b"x" * 1000)
    store = _Store(["pakdata_20261113_200000.dump.enc"])   # size 1 = a broken earlier upload
    out = ob.run(store=store, redis_client=_Redis())
    assert out["uploaded"] and [a["size"] for a in store.assets_] == [ob.encrypted_size(1000)]


def test_offsite_freshness_alert(env, monkeypatch):
    du = namedtuple("du", "total used free")(100, 10, 90)
    redis, alerts = _Redis(), []
    monkeypatch.setattr(sched, "_disk_usage", lambda p: du)
    monkeypatch.setattr(sched, "_meminfo", lambda: {})
    monkeypatch.setattr(sched, "_last_backup", lambda: None)
    monkeypatch.setattr(sched, "get_redis", lambda: redis)
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: alerts.append(t))

    assert any(p.startswith("offsite") and "never recorded" in p for p in sched.resource_check())
    redis[ob.REDIS_KEY] = f"{int(time.time()) - 3600} pakdata_x.dump.enc"
    assert sched.resource_check() == []
    redis[ob.REDIS_KEY] = f"{int(time.time()) - 80 * 3600} pakdata_x.dump.enc"
    assert any(p.startswith("offsite") for p in sched.resource_check())
    assert alerts.count("Off-site DB backup is stale") == 2     # re-armed after the fresh hour
