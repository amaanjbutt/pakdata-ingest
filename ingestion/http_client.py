"""Shared ingestion HTTP client with browser-TLS impersonation.

Several official sources (SBP, SBP EasyData, MUFAP) sit behind Cloudflare, which
fingerprints and 403s plain Python TLS clients. `curl_cffi` impersonates a real
browser's TLS/HTTP2 fingerprint, which restores access for a client that holds a
legitimate API key / consumes public official data. Requests stay paced by the
existing quota/scheduler logic — impersonation is only about not being
mis-classified, not about hammering the source.

Egress: some hosts additionally block by IP reputation (datacenter ranges). For
those, set `INGEST_PROXY` (e.g. a WARP/residential SOCKS proxy) and list the hosts
that should use it in `INGEST_PROXY_HOSTS` (comma-separated substrings). Hosts not
listed go direct. Falls back to httpx if curl_cffi is unavailable.
"""
from __future__ import annotations

import os
import time
from urllib.parse import urlsplit

from app.config import settings

try:  # curl_cffi is optional; degrade to httpx if missing
    from curl_cffi import requests as _cr

    _HAVE_CFFI = True
except Exception:  # pragma: no cover
    _HAVE_CFFI = False

import httpx

_IMPERSONATE = os.getenv("INGEST_IMPERSONATE", "chrome")

# Single-proxy form (legacy): INGEST_PROXY + INGEST_PROXY_HOSTS (comma list).
_PROXY = os.getenv("INGEST_PROXY") or None
_PROXY_HOSTS = [h.strip() for h in os.getenv("INGEST_PROXY_HOSTS", "").split(",") if h.strip()]

# Per-host map form: INGEST_PROXY_MAP = "host_substr=proxy_url,host_substr=proxy_url".
# First matching host wins. Lets different blocked sources egress differently —
# e.g. MUFAP via WARP (always-on), EasyData/SBP via the residential reverse tunnel.
def _parse_map(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            host, proxy = pair.split("=", 1)
            if host.strip() and proxy.strip():
                out.append((host.strip(), proxy.strip()))
    return out


_PROXY_MAP = _parse_map(os.getenv("INGEST_PROXY_MAP", ""))


def _proxies_for(url: str):
    host = urlsplit(url).hostname or ""
    for host_substr, proxy in _PROXY_MAP:
        if host_substr in host:
            return {"http": proxy, "https": proxy}
    if _PROXY and any(h in host for h in _PROXY_HOSTS):
        return {"http": _PROXY, "https": _PROXY}
    return None


def _retry_after(resp, default: int) -> int:
    try:
        return min(int(resp.headers.get("retry-after", default)), 60)
    except (TypeError, ValueError):
        return default


def get(url: str, timeout: int = 30):
    """GET returning a response with `.content` / `.text` / `.status_code`.

    Backs off on 429 (source rate-limit) rather than failing the run — official
    sources throttle bursty callers, so we honour Retry-After and retry twice."""
    for attempt in range(3):
        if _HAVE_CFFI:
            resp = _cr.get(url, impersonate=_IMPERSONATE, proxies=_proxies_for(url), timeout=timeout)
        else:
            resp = httpx.get(
                url, headers={"User-Agent": settings.user_agent}, timeout=timeout,
                follow_redirects=True,
            )
        if resp.status_code == 429 and attempt < 2:
            time.sleep(_retry_after(resp, 10))
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def post(url: str, json=None, timeout: int = 60):
    """POST JSON returning a response with `.text` / `.json()` / `.status_code`."""
    if _HAVE_CFFI:
        resp = _cr.post(
            url, json=json, impersonate=_IMPERSONATE, proxies=_proxies_for(url), timeout=timeout
        )
        resp.raise_for_status()
        return resp
    resp = httpx.post(
        url, json=json, headers={"User-Agent": settings.user_agent}, timeout=timeout
    )
    resp.raise_for_status()
    return resp
