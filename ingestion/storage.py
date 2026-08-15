"""Raw archive: every fetched source file is stored immutably before parsing
(PRD §4 raw archive, §5.2 archive-first). Local filesystem for MVP; the same
interface can be backed by S3/minio later.
"""
from __future__ import annotations

import os
import re
from datetime import date

from app.config import settings


def _safe_component(c: str) -> str:
    """Sanitize a single path component so it cannot escape the archive dir.

    Drops any directory parts, restricts to a safe charset, and rejects
    empty/'.'/'..'. `source`, `job`, and `filename` may be derived from config
    ids (or, in future, remote content), so a '..' must never slip through.
    """
    c = str(c).replace("\\", "/").split("/")[-1]  # drop any path parts
    c = re.sub(r"[^A-Za-z0-9._-]", "_", c)
    if c in ("", ".", ".."):
        raise ValueError(f"unsafe path component: {c!r}")
    return c


def archive_path(source: str, job: str, when: date, filename: str) -> str:
    path = os.path.join(
        settings.raw_archive_dir,
        _safe_component(source),
        _safe_component(job),
        when.isoformat(),  # ISO date is already safe
        _safe_component(filename),
    )
    # Defense in depth: ensure the resolved path stays inside the base dir.
    base = os.path.realpath(settings.raw_archive_dir)
    full = os.path.realpath(path)
    if not (full == base or full.startswith(base + os.sep)):
        raise ValueError("archive path escapes base dir")
    return path


def archive(source: str, job: str, when: date, filename: str, content: bytes) -> str:
    """Write raw content to raw/{source}/{job}/{date}/{filename}. Returns the path."""
    path = archive_path(source, job, when, filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path
