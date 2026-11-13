"""Host resource sweep (scheduler.resource_check) and the backup rotation script."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections import namedtuple
from datetime import date, timedelta
from pathlib import Path

import pytest

import ingestion.scheduler as sched

_DU = namedtuple("_DU", "total used free")
GB = 2**30


class _Redis:
    def __init__(self, used=10 * 2**20, cap=256 * 2**20):
        self.store: dict[str, str] = {}
        self.mem = {"used_memory": used, "maxmemory": cap}

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    def delete(self, k):
        return 1 if self.store.pop(k, None) is not None else 0

    def info(self, section=None):
        return dict(self.mem)


@pytest.fixture
def box(monkeypatch, tmp_path):
    """A healthy box: 40% disk, 60% memory available, idle swap, small Redis, fresh backup."""
    state = {"du": _DU(48 * GB, 19 * GB, 29 * GB),
             "mem": {"MemTotal": 4_000_000, "MemAvailable": 2_400_000,
                     "SwapTotal": 2_000_000, "SwapFree": 1_900_000},
             "redis": _Redis(), "alerts": []}
    monkeypatch.setattr(sched, "_disk_usage", lambda p: state["du"])
    monkeypatch.setattr(sched, "_meminfo", lambda: state["mem"])
    monkeypatch.setattr(sched, "get_redis", lambda: state["redis"])
    monkeypatch.setattr(sched.alerting, "alert", lambda t, m: state["alerts"].append(t))
    monkeypatch.setattr(sched, "BACKUP_DIR", str(tmp_path))
    (tmp_path / "last_ok").write_text(f"{int(time.time()) - 3600} x pakdata_x.dump 123\n")
    state["dir"] = tmp_path
    return state


def test_healthy_box_is_quiet(box):
    assert sched.resource_check() == []
    assert box["alerts"] == []


def test_disk_alert_throttled_then_rearmed(box):
    box["du"] = _DU(48 * GB, 41 * GB, 7 * GB)          # 85%
    assert [p.split(":")[0] for p in sched.resource_check()] == ["disk"]
    sched.resource_check()                               # same problem next hour: no second mail
    assert box["alerts"] == ["VPS disk 85% full"]
    box["du"] = _DU(48 * GB, 20 * GB, 28 * GB)          # cleared → re-armed
    assert sched.resource_check() == []
    box["du"] = _DU(48 * GB, 44 * GB, 4 * GB)
    sched.resource_check()
    assert len(box["alerts"]) == 2


def test_low_memory_or_heavy_swap_alerts(box):
    box["mem"] = {**box["mem"], "MemAvailable": 200_000}                 # 5% available
    assert any(p.startswith("memory") for p in sched.resource_check())
    box["mem"] = {**box["mem"], "MemAvailable": 2_000_000, "SwapFree": 200_000}  # swap 90% used
    box["redis"].store.clear()
    assert any(p.startswith("memory") for p in sched.resource_check())


def test_redis_near_cap_alerts(box):
    box["redis"].mem["used_memory"] = 240 * 2**20
    assert any(p.startswith("redis") for p in sched.resource_check())


def test_stale_backup_marker_alerts(box):
    (box["dir"] / "last_ok").write_text(f"{int(time.time()) - 40 * 3600} x pakdata_old.dump 1\n")
    found = sched.resource_check()
    assert any(p.startswith("backup") and "pakdata_old.dump" in p for p in found)


def test_backup_falls_back_to_newest_dump_without_marker(box):
    (box["dir"] / "last_ok").unlink()
    dump = box["dir"] / "pakdata_20261111_202710.dump"
    dump.write_bytes(b"x")
    assert sched.resource_check() == []                  # fresh mtime → fine
    old = time.time() - 50 * 3600
    os.utime(dump, (old, old))
    assert any(p.startswith("backup") for p in sched.resource_check())


def test_no_backup_dir_skips_backup_check(box, monkeypatch):
    monkeypatch.setattr(sched, "BACKUP_DIR", str(box["dir"] / "missing"))
    assert sched.resource_check() == []


def test_health_check_survives_a_broken_resource_check(monkeypatch):
    monkeypatch.setattr(sched, "stuck_run_check", lambda: [])
    monkeypatch.setattr(sched, "staleness_check", lambda: [])
    monkeypatch.setattr(sched, "resource_check", lambda: 1 / 0)
    out = sched.health_check()
    assert out["resources"] and "resource check failed" in out["resources"][0]


# ---- backup_rotate.sh ----------------------------------------------------------

ROTATE = Path(__file__).resolve().parents[1] / "scripts" / "backup_rotate.sh"


def _gnu_date() -> bool:
    try:
        return subprocess.run(["date", "-u", "-d", "20261111", "+%G-W%V"], capture_output=True,
                              text=True, timeout=5).stdout.strip() == "2026-W46"
    except (OSError, subprocess.SubprocessError):
        return False


needs_sh = pytest.mark.skipif(not (shutil.which("sh") and _gnu_date()), reason="needs sh + GNU date")


def _rotate(d: Path, *args: str, **env: str) -> list[str]:
    subprocess.run(["sh", str(ROTATE), *args, str(d)], check=True, capture_output=True,
                   env={**os.environ, **env}, timeout=60)
    return sorted(p.name for p in d.iterdir())


@needs_sh
def test_rotate_keeps_daily_weekly_monthly(tmp_path):
    newest = date(2026, 11, 11)                          # a Wednesday
    for i in range(120):
        (tmp_path / f"pakdata_{(newest - timedelta(days=i)):%Y%m%d}_202710.dump").write_bytes(b"x")
    (tmp_path / "pakdata_20261111_093000.dump").write_bytes(b"x")   # a second dump the same day
    (tmp_path / "last_ok").write_text("1 x y 1\n")
    (tmp_path / "pakdata_manual_copy.dump").write_bytes(b"x")      # not ours to rotate

    kept = _rotate(tmp_path, BACKUP_KEEP_DAILY="7", BACKUP_KEEP_WEEKLY="4", BACKUP_KEEP_MONTHLY="3")
    assert kept == sorted([
        "last_ok", "pakdata_manual_copy.dump",
        *(f"pakdata_202611{d:02d}_202710.dump" for d in range(5, 12)),      # 7 daily
        "pakdata_20261101_202710.dump", "pakdata_20261025_202710.dump",     # weekly (Sun = end of ISO week)
        "pakdata_20261031_202710.dump", "pakdata_20260930_202710.dump",     # monthly
    ])                                                   # Nov 8 (weekly) is already a daily


@needs_sh
def test_rotate_dry_run_deletes_nothing(tmp_path):
    for i in range(20):
        (tmp_path / f"pakdata_{(date(2026, 11, 11) - timedelta(days=i)):%Y%m%d}_200000.dump").write_bytes(b"x")
    assert len(_rotate(tmp_path, "--dry-run", BACKUP_KEEP_DAILY="2", BACKUP_KEEP_WEEKLY="0",
                       BACKUP_KEEP_MONTHLY="0")) == 20
    assert len(_rotate(tmp_path, BACKUP_KEEP_DAILY="2", BACKUP_KEEP_WEEKLY="0", BACKUP_KEEP_MONTHLY="0")) == 2
