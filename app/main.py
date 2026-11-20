from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (account, admin, agri, amcs, auth, billing, bulk, calendar, catalog, charts,
                     commodities, derived, downloads, fixed_income, funds, ports, regional, search,
                     securities, series, status, trade, usage, webhooks)

app = FastAPI(
    title="PakDataHub API",
    version="1.0.0",
    description="Self-serve REST API for Pakistani financial & economic data.",
)

app.include_router(catalog.router)
app.include_router(commodities.router)
app.include_router(agri.router)
app.include_router(fixed_income.router)
app.include_router(funds.router)
app.include_router(amcs.router)
app.include_router(securities.router)
app.include_router(trade.router)
app.include_router(ports.router)
app.include_router(regional.router)
app.include_router(derived.router)
app.include_router(series.router)
app.include_router(charts.router)
app.include_router(downloads.router)
app.include_router(bulk.router)
app.include_router(status.router)
app.include_router(usage.router)
app.include_router(billing.router)
app.include_router(search.router)
app.include_router(calendar.router)
app.include_router(webhooks.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(auth.router)


@app.middleware("http")
async def csv_everywhere(request: Request, call_next):
    """`?format=csv` on any list endpoint: endpoints with their own CSV (series, mandi, ports) answer
    it themselves; for the rest the JSON `data` rows become CSV here (nested fields as dotted columns)."""
    response = await call_next(request)
    if (request.method != "GET" or request.query_params.get("format") != "csv"
            or not request.url.path.startswith("/v1/") or response.status_code != 200
            or not response.headers.get("content-type", "").startswith("application/json")):
        return response
    import json as _json

    from fastapi.responses import Response as _Response

    from app.services.tabular import rows_to_csv

    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        payload = _json.loads(body)
    except ValueError:
        payload = None
    rows = payload.get("data") if isinstance(payload, dict) else None
    headers = {k: v for k, v in response.headers.items()
               if k.lower() not in ("content-length", "content-type")}
    if not isinstance(rows, list):
        return JSONResponse(status_code=422, headers=headers, content={
            "success": False, "error": {"code": "invalid_params",
                                        "message": "format=csv isn't available for this endpoint (it has no rows)"}})
    return _Response(rows_to_csv(rows), media_type="text/csv; charset=utf-8", headers=headers)


@app.middleware("http")
async def ratelimit_headers(request: Request, call_next):
    import time

    from app.services import metrics, reqlog

    start = time.perf_counter()
    ctx = reqlog.begin()
    response = await call_next(request)
    ms = (time.perf_counter() - start) * 1000
    # Record real latency/availability for the status page (best-effort). Exclude
    # /v1/status itself — its heavy aggregation query would skew its own p95.
    path = request.url.path
    if path.startswith("/v1/") and path != "/v1/status":
        metrics.record(ms, response.status_code)
    # Per-request log (endpoint, caller, status, latency) → Redis stream → api_requests.
    reqlog.record(request, response, ms, ctx)
    remaining = getattr(request.state, "ratelimit_remaining", None)
    if remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Limit"] = str(
            getattr(request.state, "ratelimit_limit", "")
        )
        # The day's allowance (1 call = 1 request; resets 00:00 UTC).
        response.headers["X-RateLimit-Day-Limit"] = str(getattr(request.state, "ratelimit_day_limit", ""))
        response.headers["X-RateLimit-Day-Remaining"] = str(
            getattr(request.state, "ratelimit_day_remaining", ""))
    caller = getattr(request.state, "caller", None)
    if caller is not None and path.startswith("/v1/"):
        # Most points one call returns on this plan (1,000 free, 5,000 paid).
        response.headers["X-Page-Max"] = str(caller.limits.max_page)
    return response

# Map internal detail strings to the PRD error envelope (§6).
_CODE_BY_STATUS = {
    401: "unauthorized",
    402: "subscription_inactive",
    403: "tier_forbidden",
    404: "not_found",
    422: "invalid_params",
    429: "rate_limited",
}


@app.exception_handler(StarletteHTTPException)
def http_exception_handler(request: Request, exc: StarletteHTTPException):
    # An exception may carry an explicit `error_code` (e.g. credits_exhausted) to
    # override the coarse status→code map; otherwise fall back to the status code.
    code = getattr(exc, "error_code", None) or _CODE_BY_STATUS.get(exc.status_code, "error")
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": {"code": code, "message": str(exc.detail)}},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {"code": "invalid_params", "message": str(exc.errors())},
        },
    )


_INDEX = {
    "success": True,
    "name": "PakDataHub API",
    "docs": "https://pakdatahub.com/docs",
    "reference": "https://api.pakdatahub.com/docs",
    "openapi": "https://api.pakdatahub.com/openapi.json",
    "status": "https://api.pakdatahub.com/v1/status",
    "get_a_key": "https://pakdatahub.com/signup",
}


@app.get("/", include_in_schema=False)
@app.get("/v1", include_in_schema=False)
@app.get("/v1/", include_in_schema=False)
def index(request: Request):
    """The bare host used to 404. A browser goes to the docs; an API client gets the links."""
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("https://pakdatahub.com/docs", status_code=302)
    return _INDEX


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}
