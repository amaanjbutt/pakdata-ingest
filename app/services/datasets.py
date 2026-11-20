"""One series of any kind — a catalog series, a fund's NAV, a security's price, a mandi market's
price — as plain rows, for the website's full-history charts (app/api/charts.py) and its
downloads (app/api/downloads.py).

`load(kind, ...)` returns a `Dataset`: metadata (name, unit, frequency, source, page URL) and
every row ascending, each with `date`, `value` (the charted figure) and any extra columns the
file carries. `chart_points()` turns the rows into what a chart may show: the last 5 years at
published precision (daily series thinned to one point a week) and older years as period
averages, so reading a chart never gives back the published daily/monthly figures.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from fastapi import HTTPException

from app import db
from app.services import series_repo

SITE = "https://pakdatahub.com"

# A chart carries at most this many points: 5 years weekly (≈ 260) + older history averaged — monthly
# for about 20 years of a daily series, annual beyond that.
CHART_BUDGET = 500
RECENT_YEARS = 5


@dataclass
class Dataset:
    kind: str                 # series | fund | security | agri
    subject: str              # series id, fund id, security code, "commodity/market"
    name: str
    unit: str
    frequency: str
    source: str
    page: str                 # the PakDataHub page for this series
    rows: list[dict] = field(default_factory=list)   # ascending; date, value (+ dims, extras)
    columns: list[str] = field(default_factory=list)  # extra columns, in file order
    dims_keys: list[str] = field(default_factory=list)
    filename: str = "pakdatahub"
    vintage: date | None = None


def _num(v) -> float | None:
    return None if v is None else float(v)


# ---- loaders --------------------------------------------------------------------------

def _pick_default_group(rows: list[dict]) -> dict | None:
    """A series with several dims groups (KIBOR bid/offer, prices by city) is charted one group
    at a time: the conventional one (offer, national, total, sell) else the first value."""
    keys: dict[str, set[str]] = {}
    for r in rows:
        for k, v in (r.get("dims") or {}).items():
            keys.setdefault(k, set()).add(str(v))
    out = {}
    for k, vals in keys.items():
        if len(vals) < 2:
            continue
        out[k] = next((p for p in ("offer", "national", "total", "sell") if p in vals), sorted(vals)[0])
    return out or None


def load_series(series_id: str, dims: dict[str, str] | None = None, *, one_group: bool = False,
                transform: str | None = None, vintage: date | None = None) -> Dataset:
    meta = series_repo.get_series(series_id)
    if not meta:
        raise HTTPException(status_code=404, detail="unknown series")
    if vintage is not None:
        rows = series_repo.get_observations_asof(series_id, vintage, dims=dims, limit=10_000_000, sort="asc")
    else:
        rows = series_repo.get_observations(series_id, dims=dims, limit=10_000_000, sort="asc")
    if one_group and not dims:
        group = _pick_default_group(rows)
        if group:
            rows = [r for r in rows if all(str((r.get("dims") or {}).get(k)) == v for k, v in group.items())]
    unit = meta.get("unit") or ""
    if transform:
        from app.services import transforms

        try:
            transforms.validate(transform, meta["frequency"])
            rows = transforms.apply_transform(rows, transform, meta["frequency"])
        except transforms.TransformError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        unit = transforms.transformed_unit(transform, unit) or unit
    dims_keys = sorted({k for r in rows for k in (r.get("dims") or {})})
    out = [{"date": r["obs_date"], "value": _num(r["value"]), "dims": r.get("dims") or {}} for r in rows]
    source = "PakDataHub (derived from official series)" if meta.get("derivation") else meta.get("source") or ""
    return Dataset(kind="series", subject=series_id, name=meta["name"], unit=unit,
                   frequency=meta.get("frequency") or "", source=source, page=f"{SITE}/series/{series_id}",
                   rows=out, dims_keys=dims_keys, filename=f"pakdatahub_{series_id}", vintage=vintage)


def load_fund(fund_id: int) -> Dataset:
    fund = series_repo.get_fund(fund_id)
    if not fund:
        raise HTTPException(status_code=404, detail="unknown fund")
    rows = series_repo.fund_navs(fund_id, limit=10_000_000, sort="asc")
    out = [{"date": r["obs_date"], "value": _num(r["nav"]), "offer": _num(r.get("offer")),
            "repurchase": _num(r.get("repurchase"))} for r in rows if r.get("nav") is not None]
    return Dataset(kind="fund", subject=str(fund_id), name=f"{fund.get('name') or fund_id} — NAV",
                   unit="pkr", frequency="daily", source=f"MUFAP · {fund.get('amc') or ''}".rstrip(" ·"),
                   page=f"{SITE}/funds/{fund_id}", rows=out, columns=["offer", "repurchase"],
                   filename=f"pakdatahub_fund_{fund_id}_nav")


def load_security(code: str) -> Dataset:
    sec = series_repo.get_security(code)
    if not sec:
        raise HTTPException(status_code=404, detail="unknown security")
    rows = series_repo.security_prices(code, limit=10_000_000, sort="asc")
    out = [{"date": r["obs_date"], "value": _num(r["price"]), "net_change": _num(r.get("net_change"))}
           for r in rows if r.get("price") is not None]
    safe = "".join(ch if ch.isalnum() else "_" for ch in code)
    return Dataset(kind="security", subject=code, name=f"{sec.get('name') or code} — price",
                   unit="pkr_per_100", frequency="daily", source="MUFAP",
                   page=f"{SITE}/securities/{code}", rows=out, columns=["net_change"],
                   filename=f"pakdatahub_security_{safe}_prices")


def load_agri(commodity: str, market: str) -> Dataset:
    from app.api.agri import SOURCES, _resolve

    c = _resolve("agri_commodities", "commodity", commodity)
    m = _resolve("agri_markets", "market", market)
    rows = db.query(
        "SELECT obs_date, min_price, max_price, fqp, quantity FROM agri_prices "
        "WHERE commodity_id = %s AND market_id = %s ORDER BY obs_date",
        (c["commodity_id"], m["market_id"]))
    out = [{"date": r["obs_date"], "value": _num(r["fqp"]), "min": _num(r.get("min_price")),
            "max": _num(r.get("max_price")), "quantity": _num(r.get("quantity"))}
           for r in rows if r.get("fqp") is not None]
    cname = c.get("display_name") or c.get("name")
    mname = m.get("display_name") or m.get("name")
    return Dataset(kind="agri", subject=f"{c['slug']}/{m['slug']}", name=f"{cname} — {mname}",
                   unit=c.get("unit") or "", frequency="daily",
                   source=SOURCES.get(c.get("province") or "Punjab", SOURCES["Punjab"]),
                   page=f"{SITE}/mandi/{c.get('crop_key') or c['slug']}?market={m['slug']}", rows=out,
                   columns=["min", "max", "quantity"], filename=f"pakdatahub_mandi_{c['slug']}_{m['slug']}")


# ---- charts -------------------------------------------------------------------------------

_SUB_WEEKLY = {"daily", "business_daily", "business daily"}
_WEEKLY = {"weekly", "fortnightly"}


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:
        return d.replace(year=d.year - years, day=28)


def _round(v: float) -> float:
    return float(f"{v:.6g}")


def _average(rows: list[dict], period: str) -> list[dict]:
    """Average `value` per calendar month or year; the point is dated the period's first day."""
    buckets: dict[date, list[float]] = {}
    for r in rows:
        if r["value"] is None:
            continue
        d = r["date"]
        key = date(d.year, d.month, 1) if period == "month" else date(d.year, 1, 1)
        buckets.setdefault(key, []).append(r["value"])
    return [{"date": k, "value": _round(sum(v) / len(v)), "avg": period}
            for k, v in sorted(buckets.items())]


def _weekly(rows: list[dict]) -> list[dict]:
    """One published value per ISO week: the week's last observation."""
    last: dict[tuple[int, int], dict] = {}
    for r in rows:
        if r["value"] is None:
            continue
        iso = r["date"].isocalendar()
        last[(iso[0], iso[1])] = r
    return [{"date": r["date"], "value": r["value"]} for _, r in sorted(last.items())]


def chart_points(rows: list[dict], frequency: str) -> dict:
    """What a full-history chart may show. Returns {points, recent_from, older}:
    `points` ascending, each {date, value, avg?}; `avg` = "month" | "year" on averaged points."""
    rows = [r for r in rows if r.get("value") is not None]
    if not rows:
        return {"points": [], "recent_from": None, "older": None}
    freq = (frequency or "").lower()
    cutoff = _years_before(rows[-1]["date"], RECENT_YEARS)
    recent = [r for r in rows if r["date"] >= cutoff]
    older = [r for r in rows if r["date"] < cutoff]
    if freq in _SUB_WEEKLY:
        recent_pts = _weekly(recent)
    else:
        recent_pts = [{"date": r["date"], "value": r["value"]} for r in recent]
    room = max(CHART_BUDGET - len(recent_pts), 40)
    older_kind: str | None = None
    if not older:
        older_pts: list[dict] = []
    elif freq in _SUB_WEEKLY or freq in _WEEKLY:
        older_pts = _average(older, "month")
        older_kind = "month"
        if len(older_pts) > room:
            older_pts, older_kind = _average(older, "year"), "year"
    elif freq == "monthly":
        older_pts, older_kind = _average(older, "year"), "year"
    elif freq == "quarterly" and len(older) > room:
        older_pts, older_kind = _average(older, "year"), "year"
    else:
        # annual, half-yearly, short quarterly, census, survey, irregular: exact (small, and
        # printed in every yearbook anyway)
        older_pts = [{"date": r["date"], "value": r["value"]} for r in older]
    return {"points": older_pts + recent_pts, "recent_from": cutoff, "older": older_kind}


# ---- files -----------------------------------------------------------------------------------

def citation(ds: Dataset, today: date | None = None) -> str:
    today = today or date.today()
    as_of = f" as of {ds.vintage.isoformat()}" if ds.vintage else ""
    return (f"{ds.name}{as_of}. Source: {ds.source}, via PakDataHub ({ds.page}). "
            f"Retrieved {today.isoformat()}. Please credit PakDataHub with a link.")


def _cell(v: Any) -> Any:
    if isinstance(v, date):
        return v.isoformat()
    return "" if v is None else v


def _csv_safe(v) -> str:
    """Neutralize CSV formula injection (cells starting = + - @ tab CR) — but keep negative
    numbers numeric."""
    s = "" if v is None else str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(s)
            return s
        except ValueError:
            return "'" + s
    return s


def header(ds: Dataset) -> list[str]:
    return ["date", *ds.dims_keys, "value", *ds.columns]


def table(ds: Dataset):
    for r in ds.rows:
        dims = r.get("dims") or {}
        yield [r["date"], *[dims.get(k, "") for k in ds.dims_keys], r["value"], *[r.get(c) for c in ds.columns]]


def to_csv(ds: Dataset) -> bytes:
    import csv
    import io

    buf = io.StringIO()
    buf.write("# " + citation(ds).replace("\n", " ") + "\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header(ds))
    for row in table(ds):
        w.writerow([_csv_safe(_cell(v)) for v in row])
    return buf.getvalue().encode("utf-8")


def to_xlsx(ds: Dataset) -> bytes:
    import io

    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    sheet = wb.create_sheet("Data")
    sheet.append(header(ds))
    for row in table(ds):
        sheet.append([("'" + v) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in row])
    about = wb.create_sheet("About")
    for k, v in (("Series", ds.name), ("Unit", ds.unit), ("Frequency", ds.frequency), ("Source", ds.source),
                 ("Page", ds.page), ("As of", ds.vintage.isoformat() if ds.vintage else "latest"),
                 ("Retrieved", date.today().isoformat()), ("Cite as", citation(ds))):
        about.append([k, v])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def dims_json(d: dict | None) -> str:
    return json.dumps(d or {}, separators=(",", ":"), sort_keys=True)


def recent_rows(ds: Dataset, n: int) -> list[dict]:
    return [{"date": r["date"], "value": r["value"], **({"dims": r["dims"]} if r.get("dims") else {})}
            for r in ds.rows[-n:]][::-1]
