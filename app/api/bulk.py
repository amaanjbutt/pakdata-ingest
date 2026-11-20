"""Whole-dataset files: every series of a dataset, full history, in one file.

    GET /v1/bulk                         every dataset file (name, size, rows, built) — links on
                                         Pro and Business
    GET /v1/bulk/{dataset}?format=       a link to one file (csv.gz or xlsx), valid 10 minutes

A dataset is every series sharing the first part of its id: `rates` (KIBOR, policy rate,
yields), `remittances` (every corridor), `bop` (balance of payments) … Files are rebuilt nightly
when the data changed. Auth: a session (the website) or an API key; a call with a key counts
as one call.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app import db
from app.security import Caller, authenticate_session_or_key
from app.services import r2

router = APIRouter(prefix="/v1/bulk", tags=["bulk"])

LINK_TTL = 600


def _label(dataset: str) -> str:
    from scripts.bulk_files import label

    return label(dataset)


def _files() -> list[dict]:
    return db.query("SELECT dataset, format, object_key, bytes, series_count, rows, built_at "
                    "FROM bulk_files ORDER BY dataset, format")


def _upgrade_detail() -> str:
    return "whole-dataset files come with the Pro and Business plans"


@router.get("")
def list_bulk(caller: Caller = Depends(authenticate_session_or_key)):
    """Every whole-dataset file. `url` is set (valid 10 minutes) when your plan includes them."""
    allowed = caller.limits.bulk and r2.configured()
    out: dict[str, dict] = {}
    for f in _files():
        d = out.setdefault(f["dataset"], {"dataset": f["dataset"], "name": _label(f["dataset"]),
                                          "series": f["series_count"], "rows": f["rows"],
                                          "built_at": f["built_at"], "files": []})
        name = f["object_key"].rsplit("/", 1)[-1]
        d["files"].append({"format": f["format"], "bytes": f["bytes"],
                           "url": r2.presign_get(f["object_key"], LINK_TTL, filename=name) if allowed else None})
    return {"success": True, "included": caller.limits.bulk, "data": list(out.values()),
            "count": len(out), **({} if caller.limits.bulk else {"upgrade": _upgrade_detail()})}


@router.get("/{dataset}")
def bulk_link(dataset: str, format: str = Query("csv.gz", pattern=r"^(csv\.gz|xlsx)$"),
              caller: Caller = Depends(authenticate_session_or_key)):
    """A short-lived link to one dataset file."""
    row = db.query_one("SELECT object_key, bytes, rows, series_count, built_at FROM bulk_files "
                       "WHERE dataset = %s AND format = %s", (dataset, format))
    if not row:
        raise HTTPException(status_code=404, detail=f"no {format} file for dataset {dataset!r}")
    if not caller.limits.bulk:
        raise HTTPException(status_code=403, detail=_upgrade_detail())
    if not r2.configured():
        raise HTTPException(status_code=503, detail="whole-dataset files are unavailable right now")
    name = row["object_key"].rsplit("/", 1)[-1]
    try:
        db.execute("INSERT INTO downloads (user_id, kind, subject, format) VALUES (%s, 'bulk', %s, %s)",
                   (caller.user_id, dataset, format))
    except Exception:  # noqa: BLE001
        pass
    return {"success": True, "dataset": dataset, "name": _label(dataset), "format": format,
            "bytes": row["bytes"], "rows": row["rows"], "series": row["series_count"],
            "built_at": row["built_at"], "url": r2.presign_get(row["object_key"], LINK_TTL, filename=name),
            "expires_in": LINK_TTL}
