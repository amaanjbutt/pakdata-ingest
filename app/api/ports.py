"""Ports — every ship in Port Qasim's and Karachi Port's daily statements (`port_vessel_calls`).

    GET /v1/ports/vessels?port=&date=&section=           one day's statement, ship by ship (keyed)
    GET /v1/ports/vessels?vessel=&port=&from=&to=        one ship across days (keyed)
    GET /v1/ports/dates?port=                            the statement dates on file (public)

`port` is PQA (Port Qasim, daily since 31 May 2019) or KPT (Karachi Port, daily since November 2026).
Without `date` (and without `vessel`) the latest statement is returned. `section`: berth (alongside, with
cargo handled in the last 24 hours), anchorage (waiting at the outer anchorage, PQA), expected (PQA),
sailed (PQA, previous day), containers (KPT, TEUs per container ship), programme (KPT, ships expected and
waiting), calls (KPT, every ship call since 1 May 2026, dated by its arrival day: arrival and departure
times, purpose and cargo). Each row is one ship × commodity × direction on one statement date, so following
a ship across dates shows its cargo worked down — and any manifest or balance the port restated. The daily
totals are ordinary series (`ports.qasim.*`, `ports.karachi.*`).
"""
from __future__ import annotations

import csv
import io
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app import db
from app.security import Caller, authenticate, history_floor
from app.services import cache

router = APIRouter(prefix="/v1/ports", tags=["ports"])
MAX_LIMIT = 5000
PORTS = {"PQA": "Port Qasim Authority — Daily Shipping and Cargo Statement",
         "KPT": "Karachi Port Trust — daily tonnage and container-handling reports"}
_PORT = "^(PQA|KPT|pqa|kpt)$"
_SECTION = "^(berth|anchorage|expected|sailed|containers|programme|calls)$"
FIELDS = ("statement_date", "port", "section", "vessel", "call_ref", "direction", "commodity", "commodity_raw",
          "unit", "terminal", "berth_raw", "agent", "agent_raw", "flag", "flag_raw", "port_raw", "country",
          "vessel_type", "loa_m", "draft_m", "manifest", "handled_24h", "handled_before", "balance", "berthed_at",
          "arrived_on", "expected_at", "departs_at", "checks_ok", "details")
CSV_FIELDS = [f for f in FIELDS if f != "details"]


def _row(r: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = r.get(k)
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif v is not None and k in ("loa_m", "draft_m", "manifest", "handled_24h", "handled_before", "balance"):
            v = float(v)
        out["date" if k == "statement_date" else k] = v
    return out


@router.get("/dates")
def dates(port: str = Query("PQA", pattern=_PORT)):
    p = port.upper()
    rows = cache.cached_json(f"ports:dates:{p}:v1", 900, lambda: db.query(
        "SELECT statement_date, count(*) AS rows FROM port_vessel_calls WHERE port = %s "
        "GROUP BY statement_date ORDER BY statement_date DESC", (p,)))
    data = [{"date": r["statement_date"].isoformat() if hasattr(r["statement_date"], "isoformat")
             else r["statement_date"], "rows": r["rows"]} for r in rows]
    return {"success": True, "port": p, "source": PORTS[p], "data": data, "count": len(data)}


@router.get("/vessels")
def vessels(
    port: str | None = Query(None, pattern=_PORT, description="PQA or KPT (default PQA without vessel)"),
    on: date | None = Query(None, alias="date", description="Statement date; omit for the latest"),
    section: str | None = Query(None, pattern=_SECTION),
    vessel: str | None = Query(None, min_length=2, description="A ship's name (case-insensitive, without "
                                                                "M.V-/M.T-): its rows across statements"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    limit: int = Query(1000, ge=1, le=MAX_LIMIT),
    format: str = Query("json", pattern="^(json|csv)$"),
    caller: Caller = Depends(authenticate),
):
    clauses, params = [], {"limit": limit}
    p = port.upper() if port else (None if vessel else "PQA")
    if p:
        clauses.append("port = %(port)s")
        params["port"] = p
    floor = history_floor(caller)
    if vessel:
        clauses.append("lower(vessel) = lower(%(vessel)s)")
        params["vessel"] = vessel.strip()
        if floor is not None and (date_from is None or date_from < floor):
            date_from = floor
        if date_from:
            clauses.append("statement_date >= %(from)s")
            params["from"] = date_from
        if date_to:
            clauses.append("statement_date <= %(to)s")
            params["to"] = date_to
    else:
        day = on or (db.query_one("SELECT max(statement_date) AS d FROM port_vessel_calls WHERE port = %s",
                                  (p,)) or {}).get("d")
        if day is None:
            raise HTTPException(status_code=404, detail=f"no statements on file for {p}")
        if floor is not None and day < floor:
            raise HTTPException(status_code=403, detail=f"your plan's history starts {floor.isoformat()}")
        clauses.append("statement_date = %(day)s")
        params["day"] = day
    if section:
        clauses.append("section = %(section)s")
        params["section"] = section
    rows = db.query(
        f"SELECT * FROM port_vessel_calls WHERE {' AND '.join(clauses)} "
        "ORDER BY statement_date DESC, port, section, terminal NULLS LAST, berth_raw NULLS LAST, vessel, seq "
        "LIMIT %(limit)s", params)
    data = [_row(r) for r in rows]
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["date" if f == "statement_date" else f for f in CSV_FIELDS])
        for d in data:
            w.writerow([d["date" if f == "statement_date" else f] for f in CSV_FIELDS])
        return PlainTextResponse(buf.getvalue(), media_type="text/csv")
    return {"success": True, "port": p, "source": PORTS.get(p or "", None),
            "date": params.get("day").isoformat() if params.get("day") else None, "data": data, "count": len(data)}
