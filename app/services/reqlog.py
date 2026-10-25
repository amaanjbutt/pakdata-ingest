"""Per-request API log (who called what, how it went), cheap on the hot path.

The middleware calls `record()` after each response: one XADD to a capped Redis stream,
nothing else. The scheduler's `ingestion.request_log.flush` drains the stream into the
daily-partitioned `api_requests` table every minute; a daily rollup builds the per-endpoint
and per-dimension aggregates (kept for good), and raw days older than 30 days are exported
to private storage and dropped.

Privacy: the client IP is stored only as a keyed hash, and query strings are never stored
(they can carry `api_key=`). Logging must never fail a request, so everything here is
best-effort.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
import re
import time
from typing import Any

from app.config import settings
from app.security import get_redis

STREAM = "reqlog"
STREAM_MAXLEN = 1_000_000          # ~1-2 days of traffic if the flusher stops; trimmed approximately
_LOGGED_PREFIXES = ("/v1/", "/auth/", "/webhooks/")

# One mutable dict per request, set by the middleware. Sync endpoints run in a worker
# thread with a COPY of the context, so we share a dict (the copy points at the same
# object) rather than setting the var from inside the endpoint.
_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("reqlog_ctx", default=None)


def begin() -> dict:
    d: dict[str, Any] = {}
    _ctx.set(d)
    return d


def mark_cache(hit: bool) -> None:
    """Called by the Redis response cache; the first lookup of a request decides."""
    d = _ctx.get()
    if d is not None and "cache" not in d:
        d["cache"] = "hit" if hit else "miss"


_SALT = (os.getenv("REQLOG_SALT") or hashlib.sha256(settings.database_url.encode()).hexdigest()).encode()


def ip_hash(ip: str | None) -> str:
    if not ip:
        return ""
    return hmac.new(_SALT, ip.encode(), hashlib.sha256).hexdigest()[:16]


_BOT = re.compile(r"bot|crawl|spider|slurp|bingpreview|facebookexternalhit|embedly|"
                  r"gptbot|claudebot|ccbot|perplexity|amazonbot|bytespider|petalbot", re.I)
_SCRIPT = re.compile(r"python|httpx|requests|aiohttp|urllib|curl|wget|node|axios|undici|"
                     r"go-http|java|okhttp|postman|insomnia|r-curl|httr|powershell", re.I)


def ua_class(ua: str | None, from_site: bool = False) -> str:
    if from_site:
        return "site"
    ua = ua or ""
    if not ua:
        return "none"
    if "Google-Apps-Script" in ua:
        return "sheets"
    if re.search(r"Microsoft Office|PowerQuery|Excel|Mashup", ua, re.I):
        return "excel"
    if _BOT.search(ua):
        return "bot"
    if re.search(r"^curl/", ua, re.I):
        return "curl"
    if _SCRIPT.search(ua):
        return "script"
    if ua.startswith("Mozilla/"):
        return "browser"
    return "other"


# the query parameters that name what was asked for (never api_key / tokens)
_SUBJECT_PARAMS = ("item", "commodity", "market", "code", "country", "fund_id", "q", "module", "type", "flow")


def _subject(request) -> str:
    pp = request.path_params or {}
    parts = [str(v) for v in pp.values()]
    if not parts:
        qp = request.query_params
        parts = [f"{k}={qp[k]}" for k in _SUBJECT_PARAMS if qp.get(k)]
    return " ".join(parts)[:160]


def _client_ip(request) -> str | None:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def build(request, response, ms: float, ctx: dict | None) -> dict[str, str] | None:
    """The stream entry for one request, or None for paths we don't log."""
    path = request.url.path
    if request.method == "OPTIONS" or not path.startswith(_LOGGED_PREFIXES):
        return None
    route = request.scope.get("route")
    st = request.state
    page = (request.headers.get("x-pakdata-page") or "")[:200]
    return {
        "ts": f"{time.time():.3f}",
        "user_id": str(getattr(st, "user_id", "") or ""),
        "key_id": str(getattr(st, "key_id", "") or ""),
        "auth": str(getattr(st, "auth", "") or "public"),
        "method": request.method,
        "route": getattr(route, "path", None) or "(unmatched)",
        "subject": _subject(request),
        "status": str(response.status_code),
        "ms": f"{ms:.1f}",
        "bytes": response.headers.get("content-length", "") or "",
        "ua": ua_class(request.headers.get("user-agent"), from_site=bool(page)),
        "ip_hash": ip_hash(_client_ip(request)),
        "country": (request.headers.get("cf-ipcountry") or "")[:2],
        "cache": (ctx or {}).get("cache", ""),
        "page": page,
    }


def record(request, response, ms: float, ctx: dict | None) -> None:
    try:
        entry = build(request, response, ms, ctx)
        if entry is not None:
            get_redis().xadd(STREAM, entry, maxlen=STREAM_MAXLEN, approximate=True)
    except Exception:  # noqa: BLE001 — logging must never fail a request
        pass
