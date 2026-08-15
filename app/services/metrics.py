"""Real ops metrics for the public status page (replaces the old fixtures).

Cheap and Redis-backed: every request records its latency into a capped list and
bumps per-day request/error counters. `snapshot()` derives p95 latency and an
availability figure from that real data. Metrics must never break a request, so
every call is wrapped defensively.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.security import get_redis

_LAT_KEY = "metrics:lat"     # capped list of recent latencies (ms)
_LAT_CAP = 2000


def record(duration_ms: float, status_code: int) -> None:
    try:
        r = get_redis()
        today = date.today().isoformat()
        pipe = r.pipeline()
        pipe.lpush(_LAT_KEY, round(duration_ms, 1))
        pipe.ltrim(_LAT_KEY, 0, _LAT_CAP - 1)
        pipe.incr(f"metrics:req:{today}")
        pipe.expire(f"metrics:req:{today}", 2_764_800)  # ~32 days
        if status_code >= 500:
            pipe.incr(f"metrics:err:{today}")
            pipe.expire(f"metrics:err:{today}", 2_764_800)
        pipe.execute()
    except Exception:
        pass


def snapshot() -> dict:
    """Return {p95_ms, requests_24h, availability_pct} from real recent traffic.

    availability = share of requests that did not 5xx over the last ~24h; None
    until there's traffic to measure."""
    try:
        r = get_redis()
        lats = [float(x) for x in r.lrange(_LAT_KEY, 0, -1)]
        p95 = None
        if lats:
            lats.sort()
            p95 = round(lats[min(len(lats) - 1, int(len(lats) * 0.95))], 1)

        today = date.today()
        yday = today - timedelta(days=1)
        req = sum(int(r.get(f"metrics:req:{d}") or 0) for d in (today, yday))
        err = sum(int(r.get(f"metrics:err:{d}") or 0) for d in (today, yday))
        availability = round(100.0 * (1 - err / req), 3) if req else None
        return {"p95_ms": p95, "requests_24h": req, "availability_pct": availability}
    except Exception:
        return {"p95_ms": None, "requests_24h": 0, "availability_pct": None}
