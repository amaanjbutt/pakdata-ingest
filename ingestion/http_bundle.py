"""Pre-fetched response bundle — serve GETs from disk instead of the network.

MUFAP's Cloudflare lets only a small fraction of GitHub runner IPs through, and
even a good IP gets re-blocked after a few requests. So on a good runner we
fetch every page the MUFAP jobs need in one quick burst *before* any slow DB
work (scripts/mufap_fetch_bundle.py), and the jobs then read those pages from
here. Set INGEST_BUNDLE_DIR to a directory holding `manifest.json`
({url: filename}); any URL not in the bundle falls through to the network.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

BUNDLE_ENV = "INGEST_BUNDLE_DIR"
_MANIFEST = "manifest.json"


def _dir() -> Path | None:
    raw = os.getenv(BUNDLE_ENV, "").strip()
    return Path(raw) if raw else None


def _load_manifest(d: Path) -> dict[str, str]:
    try:
        return json.loads((d / _MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def lookup(url: str) -> bytes | None:
    """Bundled bytes for `url`, or None when there's no bundle / no entry."""
    d = _dir()
    if d is None:
        return None
    name = _load_manifest(d).get(url)
    if not name:
        return None
    try:
        return (d / name).read_bytes()
    except OSError:
        return None


def save(d: Path, url: str, content: bytes) -> str:
    """Store `content` for `url` in bundle dir `d` and record it in the manifest."""
    d.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".bin"
    (d / name).write_bytes(content)
    manifest = _load_manifest(d)
    manifest[url] = name
    (d / _MANIFEST).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return name
