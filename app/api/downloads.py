"""Site downloads: one series, full history, as CSV or Excel — the "↓ CSV / XLSX" button on
pakdatahub.com.

    POST /v1/downloads/{series_id}?format=csv|xlsx&dims=&vintage=   a catalog series
    POST /v1/downloads/fund/{fund_id}?format=                         a fund's NAV history
    POST /v1/downloads/security/{code}?format=                        a security's prices
    POST /v1/downloads/agri?commodity=&market=&format=                a mandi market's prices
    GET  /v1/downloads                                                today's count + recent files
    GET  /v1/downloads/file/{token}                                   the file (signed link)

A POST (session auth: a signed-in person on the website) counts one download — 5 a day on the
free plan, unlimited with the Data pass or any paid plan — and answers with a link that works
for 5 minutes. The link serves the file with `Content-Disposition: attachment`; its first line
(CSV) or its About sheet (Excel) carries the citation. `vintage=YYYY-MM-DD` (a series as it was
known on that date) comes with the Data pass, Pro and Business.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app import db
from app.config import settings
from app.security import (Caller, authenticate_session, download_quota, get_redis, take_download)
from app.services import datasets

router = APIRouter(prefix="/v1/downloads", tags=["website"], include_in_schema=False)

LINK_TTL = 300  # seconds a signed file link works
_FORMATS = "^(csv|xlsx)$"
_MEDIA = {"csv": "text/csv; charset=utf-8",
          "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}


def _signing_key() -> bytes:
    """DOWNLOAD_SIGNING_KEY if set, else one random key kept in Redis (links live 5 minutes, so a
    lost key only costs links already handed out)."""
    if settings.download_signing_key:
        return settings.download_signing_key.encode()
    r = get_redis()
    key = r.get("dl:signing-key")
    if not key:
        r.set("dl:signing-key", secrets.token_urlsafe(32), nx=True)
        key = r.get("dl:signing-key")
    return str(key).encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(mac)}"


def verify(token: str) -> dict:
    try:
        body, mac = token.split(".", 1)
        want = hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _unb64(mac)):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=404, detail="invalid or expired download link")
    if int(payload.get("e", 0)) < time.time():
        raise HTTPException(status_code=404, detail="this download link has expired — download again from the page")
    return payload


def _api_base(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    scheme = "https" if "pakdatahub.com" in host else request.url.scheme
    return f"{scheme}://{host}"


def _issue(request: Request, caller: Caller, kind: str, subject: str, fmt: str, params: dict) -> dict:
    quota = take_download(caller)
    exp = int(time.time()) + LINK_TTL
    token = sign({"k": kind, "s": subject, "f": fmt, "p": params, "u": caller.user_id, "e": exp})
    try:
        db.execute("INSERT INTO downloads (user_id, kind, subject, format) VALUES (%s, %s, %s, %s)",
                   (caller.user_id, kind, subject[:200], fmt))
    except Exception:  # noqa: BLE001 — the record must never block a download
        pass
    return {"success": True, "url": f"{_api_base(request)}/v1/downloads/file/{token}",
            "expires_at": datetime.fromtimestamp(exp, tz=timezone.utc).isoformat(),
            "format": fmt, "downloads": quota}


def _parse_dims(dims: str | None) -> dict[str, str] | None:
    if not dims:
        return None
    out = {}
    for pair in dims.split(","):
        if ":" not in pair:
            raise HTTPException(status_code=422, detail=f"bad dims token: {pair!r}")
        k, v = pair.split(":", 1)
        out[k.strip()] = v.strip()
    return out


@router.get("")
def my_downloads(caller: Caller = Depends(authenticate_session)):
    """Today's download count and the last 20 files, for the dashboard."""
    rows = db.query(
        "SELECT kind, subject, format, created_at FROM downloads WHERE user_id = %s "
        "ORDER BY created_at DESC LIMIT 20", (caller.user_id,))
    return {"success": True, "plan": caller.plan, "downloads": download_quota(caller), "recent": rows}


@router.post("/fund/{fund_id}")
def download_fund(request: Request, fund_id: int, format: str = Query("csv", pattern=_FORMATS),
                  caller: Caller = Depends(authenticate_session)):
    from app.services import series_repo

    if not series_repo.get_fund(fund_id):
        raise HTTPException(status_code=404, detail="unknown fund")
    return _issue(request, caller, "fund", str(fund_id), format, {})


@router.post("/security/{code:path}")
def download_security(request: Request, code: str, format: str = Query("csv", pattern=_FORMATS),
                      caller: Caller = Depends(authenticate_session)):
    from app.services import series_repo

    if not series_repo.get_security(code):
        raise HTTPException(status_code=404, detail="unknown security")
    return _issue(request, caller, "security", code, format, {})


@router.post("/agri")
def download_agri(request: Request, commodity: str = Query(...), market: str = Query(...),
                  format: str = Query("csv", pattern=_FORMATS), caller: Caller = Depends(authenticate_session)):
    from app.api.agri import _resolve

    c = _resolve("agri_commodities", "commodity", commodity)
    m = _resolve("agri_markets", "market", market)
    return _issue(request, caller, "agri", f"{c['slug']}/{m['slug']}", format,
                  {"c": str(c["commodity_id"]), "m": str(m["market_id"])})


@router.get("/file/{token}")
def download_file(token: str):
    p = verify(token)
    kind, subject, fmt, params = p["k"], p["s"], p["f"], p.get("p") or {}
    if kind == "series":
        vintage = date.fromisoformat(params["v"]) if params.get("v") else None
        ds = datasets.load_series(subject, params.get("d") or None, vintage=vintage)
    elif kind == "fund":
        ds = datasets.load_fund(int(subject))
    elif kind == "security":
        ds = datasets.load_security(subject)
    elif kind == "agri":
        ds = datasets.load_agri(params["c"], params["m"])
    else:
        raise HTTPException(status_code=404, detail="unknown download")
    body = datasets.to_xlsx(ds) if fmt == "xlsx" else datasets.to_csv(ds)
    stamp = (ds.vintage or date.today()).strftime("%Y%m%d")
    name = f"{ds.filename}_{'asof_' if ds.vintage else ''}{stamp}.{fmt}"
    return Response(content=body, media_type=_MEDIA[fmt], headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "private, no-store",
        "X-Robots-Tag": "noindex",
    })


# Registered last: `{series_id}` would otherwise swallow /fund/…, /security/…, /agri, /file/….
@router.post("/{series_id}")
def download_series(request: Request, series_id: str, format: str = Query("csv", pattern=_FORMATS),
                    dims: str | None = Query(None, description="Only these dims, e.g. side:offer; default every row"),
                    vintage: date | None = Query(None, description="The series as known on this date (pass, Pro+)"),
                    caller: Caller = Depends(authenticate_session)):
    from app.services import series_repo

    if not series_repo.get_series(series_id):
        raise HTTPException(status_code=404, detail="unknown series")
    if vintage is not None and not caller.limits.vintage_downloads:
        raise HTTPException(status_code=403,
                            detail="downloads as of a past date come with the Data pass, Pro and Business")
    params: dict = {}
    want = _parse_dims(dims)
    if want:
        params["d"] = want
    if vintage:
        params["v"] = vintage.isoformat()
    return _issue(request, caller, "series", series_id, format, params)
