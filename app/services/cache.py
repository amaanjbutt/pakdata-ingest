"""Tiny Redis JSON cache for hot, public, read-only endpoints.

The API runs on a single small VPS, so recomputing heavy aggregations (status,
catalog listings, fund/security lists) on every SSR render is the main latency
cost. These responses are identical for every caller and change slowly, so a
short-TTL cache turns repeat renders from ~1s of Postgres work into a Redis read.

Stale-while-revalidate: an entry is fresh for `ttl` seconds and kept for a grace
period after that. A read in the grace period returns the stored copy at once and
one caller (a Redis lock, so never a stampede) recomputes it in a background thread.
Nobody waits on a recompute unless the key is missing altogether — before this, the
first caller after each expiry paid the full cost (/v1/status ~650 ms every minute,
district and trade listings seconds under load).

Deleting a key (`DEL cache:v1:<key>`) still forces a fresh compute on the next read,
and a background refresh never re-creates a key that was deleted while it ran.

Fail-open: any Redis error just computes the value directly.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable, TypeVar

from app.security import get_redis
from app.services import reqlog

T = TypeVar("T")

_PREFIX = "cache:v1:"
_LOCK_SUFFIX = ":refreshing"
_LOCK_SECONDS = 60
log = logging.getLogger(__name__)


def _grace(ttl: int) -> int:
    """How long past `ttl` a copy may still be served while it is refreshed."""
    return min(max(ttl, 600), 6 * 3600)


def _store(r, rkey: str, value, ttl: int, only_if_present: bool = False) -> None:
    r.set(rkey, json.dumps(value, default=str), ex=ttl + _grace(ttl), xx=only_if_present)


def _refresh(rkey: str, ttl: int, producer: Callable[[], T]) -> None:
    try:
        value = producer()
        r = get_redis()
        # xx: if the key was deleted meanwhile (a cache bust), leave it to the next reader.
        _store(r, rkey, value, ttl, only_if_present=True)
    except Exception:  # noqa: BLE001 — a failed refresh keeps serving the stored copy
        log.exception("cache refresh failed for %s", rkey)
    finally:
        try:
            get_redis().delete(rkey + _LOCK_SUFFIX)
        except Exception:  # noqa: BLE001
            pass


# Decoded copies of large read-only values, keyed by the exact JSON they came from (shared=True).
_decoded: dict[str, tuple[str, object]] = {}


def _decode(rkey: str, raw: str, shared: bool):
    if not shared:
        return json.loads(raw)
    memo = _decoded.get(rkey)
    if memo is not None and memo[0] == raw:
        return memo[1]
    value = json.loads(raw)
    _decoded[rkey] = (raw, value)
    return value


def cached_json(key: str, ttl: int, producer: Callable[[], T], *, shared: bool = False) -> T:
    """Return producer()'s result, cached under `key` (fresh for `ttl` seconds, then
    served stale while one background refresh runs).

    shared=True: for large values the caller only reads (the trade code list is ~5 MB of JSON,
    ~30 ms to decode on the VPS): the decoded object is kept in-process and reused while the
    stored JSON is unchanged, so a bust or refresh still takes effect on the next call. The
    result is shared between requests — never mutate it."""
    rkey = _PREFIX + key
    try:
        r = get_redis()
        pipe = r.pipeline(transaction=False)
        pipe.get(rkey)
        pipe.ttl(rkey)
        hit, remaining = pipe.execute()
        reqlog.mark_cache(hit is not None)
        if hit is not None:
            # Older than `ttl` (the remaining life is inside the grace period): refresh once.
            if remaining is not None and 0 <= remaining < _grace(ttl) and \
                    r.set(rkey + _LOCK_SUFFIX, "1", nx=True, ex=_LOCK_SECONDS):
                threading.Thread(target=_refresh, args=(rkey, ttl, producer), daemon=True).start()
            return _decode(rkey, hit, shared)
    except Exception:
        r = None  # fall through to compute

    value = producer()

    try:
        if r is not None:
            _store(r, rkey, value, ttl)
    except Exception:
        pass
    return value
