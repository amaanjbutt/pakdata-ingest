"""The Redis response cache serves a stale copy while one background refresh runs (needs Redis)."""
from __future__ import annotations

import time
import uuid

import pytest

from app.services import cache


@pytest.fixture
def key():
    k = f"test:swr:{uuid.uuid4().hex}"
    yield k
    r = cache.get_redis()
    r.delete(cache._PREFIX + k, cache._PREFIX + k + cache._LOCK_SUFFIX)


def _wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_miss_then_fresh_hit(key):
    calls = []
    producer = lambda: calls.append(1) or {"v": len(calls)}  # noqa: E731
    assert cache.cached_json(key, 60, producer) == {"v": 1}
    assert cache.cached_json(key, 60, producer) == {"v": 1}
    assert len(calls) == 1
    ttl = cache.get_redis().ttl(cache._PREFIX + key)
    assert 60 < ttl <= 60 + cache._grace(60)       # kept through the grace period


def test_stale_copy_served_and_refreshed_once_in_background(key):
    r = cache.get_redis()
    rkey = cache._PREFIX + key
    cache.cached_json(key, 60, lambda: {"v": "old"})
    r.expire(rkey, 30)                              # past its 60 s freshness, inside the grace period
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
        return {"v": "new"}

    # Both readers get the stored copy at once; only one refresh starts.
    t0 = time.monotonic()
    assert cache.cached_json(key, 60, slow) == {"v": "old"}
    assert cache.cached_json(key, 60, slow) == {"v": "old"}
    assert time.monotonic() - t0 < 0.25
    assert _wait_for(lambda: cache.cached_json(key, 60, slow) == {"v": "new"})
    assert len(calls) == 1
    assert r.ttl(rkey) > 60                         # fresh again
    assert not r.exists(rkey + cache._LOCK_SUFFIX)


def test_refresh_does_not_resurrect_a_busted_key(key):
    r = cache.get_redis()
    rkey = cache._PREFIX + key
    cache.cached_json(key, 60, lambda: {"v": "old"})
    r.expire(rkey, 30)

    def slow():
        time.sleep(0.3)
        return {"v": "refreshed"}

    assert cache.cached_json(key, 60, slow) == {"v": "old"}
    r.delete(rkey)                                  # `DEL cache:v1:<key>` while the refresh runs
    assert _wait_for(lambda: not r.exists(rkey + cache._LOCK_SUFFIX))
    assert not r.exists(rkey)                       # the next reader computes it fresh
    assert cache.cached_json(key, 60, lambda: {"v": "fresh"}) == {"v": "fresh"}


def test_failed_refresh_keeps_the_stored_copy(key):
    r = cache.get_redis()
    rkey = cache._PREFIX + key
    cache.cached_json(key, 60, lambda: {"v": "old"})
    r.expire(rkey, 30)

    def boom():
        raise RuntimeError("db down")

    assert cache.cached_json(key, 60, boom) == {"v": "old"}
    assert _wait_for(lambda: not r.exists(rkey + cache._LOCK_SUFFIX))
    assert cache.cached_json(key, 60, boom) == {"v": "old"}


def test_shared_values_decode_once_and_follow_the_store(key):
    r = cache.get_redis()
    rkey = cache._PREFIX + key
    first = cache.cached_json(key, 60, lambda: {"rows": [1, 2]}, shared=True)   # computed
    a = cache.cached_json(key, 60, lambda: {"rows": []}, shared=True)
    b = cache.cached_json(key, 60, lambda: {"rows": []}, shared=True)
    assert first == a == {"rows": [1, 2]} and a is b                          # decoded once
    r.delete(rkey)                                                              # a bust
    assert cache.cached_json(key, 60, lambda: {"rows": [3]}, shared=True) == {"rows": [3]}
    assert cache.cached_json(key, 60, lambda: {"rows": []}, shared=True) == {"rows": [3]}
    cache._decoded.pop(rkey, None)
