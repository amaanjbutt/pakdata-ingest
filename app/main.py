from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (account, auth, billing, calendar, catalog, commodities,
                     fixed_income, funds, search, securities, series, status,
                     trade, usage, webhooks)

app = FastAPI(
    title="PakData API",
    version="1.0.0",
    description="Self-serve REST API for Pakistani financial & economic data.",
)

app.include_router(catalog.router)
app.include_router(commodities.router)
app.include_router(fixed_income.router)
app.include_router(funds.router)
app.include_router(securities.router)
app.include_router(trade.router)
app.include_router(series.router)
app.include_router(status.router)
app.include_router(usage.router)
app.include_router(billing.router)
app.include_router(search.router)
app.include_router(calendar.router)
app.include_router(webhooks.router)
app.include_router(account.router)
app.include_router(auth.router)


@app.middleware("http")
async def ratelimit_headers(request: Request, call_next):
    import time

    from app.services import metrics

    start = time.perf_counter()
    response = await call_next(request)
    # Record real latency/availability for the status page (best-effort). Exclude
    # /v1/status itself — its heavy aggregation query would skew its own p95.
    path = request.url.path
    if path.startswith("/v1/") and path != "/v1/status":
        metrics.record((time.perf_counter() - start) * 1000, response.status_code)
    remaining = getattr(request.state, "ratelimit_remaining", None)
    if remaining is not None:
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Limit"] = str(
            getattr(request.state, "ratelimit_limit", "")
        )
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
    code = _CODE_BY_STATUS.get(exc.status_code, "error")
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


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}
