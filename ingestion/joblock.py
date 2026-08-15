"""Global single-flight lock: at most one ingestion job runs at a time on the box.

The VPS is small (1 vCPU / ~4 GB). Running several heavy jobs at once — e.g. two
manual `--backfill` runs, or a backfill overlapping a scheduled job — can overload
it. This lock is shared by the scheduler and the manual CLI, so any second job
(scheduled or manual) waits its turn instead of piling on.

A heartbeat thread refreshes the lock while a job runs (so long backfills keep it),
and the lock auto-expires shortly after the process dies (crash-safe). If Redis is
unavailable the lock fails **open** — a safety optimisation must never itself block
ingestion.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid

import redis

from app.config import settings

log = logging.getLogger("pakdata.joblock")

_KEY = "pakdata:global_joblock"
_TTL = int(os.getenv("GLOBAL_JOB_LOCK_TTL", "900"))  # 15 min; heartbeat keeps it alive
_HEARTBEAT = max(30, _TTL // 3)

_REFRESH = ("if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end")
_DEL = ("if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end")

_redis: redis.Redis | None = None


def _r() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


class GlobalJobLock:
    def __init__(self, owner: str):
        self.owner = owner
        self.token = f"{owner}:{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.held = False

    def try_acquire(self) -> bool:
        """Take the lock if free. Returns True on success (or if Redis is down)."""
        try:
            ok = _r().set(_KEY, self.token, nx=True, ex=_TTL)
        except Exception as exc:  # fail-open — never block ingestion on the lock
            log.warning("joblock redis unavailable; proceeding without global lock: %s", exc)
            return True
        if not ok:
            return False
        self.held = True
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return True

    def holder(self) -> str | None:
        try:
            return _r().get(_KEY)
        except Exception:
            return None

    def _heartbeat(self) -> None:
        while not self._stop.wait(_HEARTBEAT):
            try:
                _r().eval(_REFRESH, 1, _KEY, self.token, _TTL)
            except Exception:
                pass

    def release(self) -> None:
        self._stop.set()
        if self.held:
            try:
                _r().eval(_DEL, 1, _KEY, self.token)
            except Exception:
                pass
        self.held = False

    def __enter__(self) -> "GlobalJobLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
