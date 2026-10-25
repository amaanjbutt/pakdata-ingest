from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel


class SeriesMeta(BaseModel):
    id: str
    module: str
    name: str
    description: str | None = None
    unit: str | None = None
    frequency: str
    source: str
    source_url: str | None = None
    dimensions: dict[str, Any] = {}
    first_date: date | None = None
    last_date: date | None = None
    tier: str = "basic"
    derived: dict[str, Any] | None = None


class Observation(BaseModel):
    date: date
    value: float | None = None
    dims: dict[str, Any] = {}


class ResponseMeta(BaseModel):
    name: str
    unit: str | None = None
    frequency: str
    source: str
    last_updated: date | None = None
    transform: str | None = None
    # Set on the PakDataHub derived layer: {"method", "inputs", "version"}; null = official.
    derived: dict[str, Any] | None = None
    # Caveats on returned points ([{date, dims, note, kind}]), e.g. a value confirmed as
    # published by the source but implausible; null when none apply.
    notes: list[dict[str, Any]] | None = None


class Pagination(BaseModel):
    next_cursor: str | None = None
    count: int


class SeriesResponse(BaseModel):
    success: bool = True
    series: str
    meta: ResponseMeta
    data: list[Observation]
    pagination: Pagination


class CatalogResponse(BaseModel):
    success: bool = True
    data: list[SeriesMeta]
    pagination: Pagination


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: bool = False
    error: ErrorDetail
