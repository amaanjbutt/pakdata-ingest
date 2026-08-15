"""Tiny Redis JSON cache for hot, public, read-only endpoints.

The API runs on a single small VPS, so recomputing heavy aggregations (status,
catalog listings, fund/security lists) on every SSR render is the main latency
cost. These responses are identical for every caller and change slowly, so a
short-TTL cache turns repeat renders from ~1s of Postgres work into a Redis read.

Fail-open: any Redis error just computes the value directly.
"""
from __future__ import annotations

import json
from typing import Callable, TypeVar

from app.security import get_redis

T = TypeVar("T")

_PREFIX = "cache:v1:"


def cached_json(key: str, ttl: int, producer: Callable[[], T]) -> T:
    """Return producer()'s result, cached under `key` for `ttl` seconds."""
    rkey = _PREFIX + key
    try:
        r = get_redis()
        hit = r.get(rkey)
        if hit is not None:
            return json.loads(hit)
    except Exception:
        r = None  # fall through to compute

    value = producer()

    try:
        if r is not None:
            r.setex(rkey, ttl, json.dumps(value, default=str))
    except Exception:
        pass
    return value
