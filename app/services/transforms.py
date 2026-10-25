"""Server-side series transforms for GET /v1/series/{id}?transform=...

Pure functions over observation rows — no DB, no I/O — so they're cheap to unit
test. A transform is applied *after* the store returns rows, grouped by the
observation's `dims` (a series can carry several parallel sub-series, e.g.
KIBOR bid/offer), computed on the ascending time order within each group.

Supported transforms:
  - ``yoy``        year-over-year % change (date-based 1-year lookback)
  - ``mom``        month-over-month % change (monthly series only)
  - ``pct_change`` period-over-period % change (previous observation)
  - ``3ma``        trailing 3-period moving average (source unit preserved)
  - ``index``      rebased to 100 at the first observation in the window

Percent transforms return values in **percent** (×100) and re-unit to
``percent``; ``index`` re-units to ``index``; ``3ma`` keeps the source unit.
Points whose partner observation is unavailable become ``null`` (honest gaps,
e.g. the first year of a yoy series).
"""
from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, timedelta

TRANSFORMS = {"yoy", "mom", "pct_change", "3ma", "index"}

# Which frequencies each transform makes sense for. `pct_change`, `3ma`, `index`
# are frequency-agnostic. `mom` is monthly-only; `yoy` needs a sub-annual series.
_YOY_OK = {"daily", "weekly", "monthly", "quarterly"}
_MOM_OK = {"monthly"}

# Nearest-prior tolerance (days) when an exact lookback date doesn't exist —
# only used for irregular daily series (weekends/holidays shift the 1-year-ago
# date off a trading day).
_YOY_TOLERANCE_DAYS = 7


class TransformError(ValueError):
    """Raised when a transform can't apply to a series (→ HTTP 422)."""


def transformed_unit(kind: str, source_unit: str | None) -> str | None:
    if kind in ("yoy", "mom", "pct_change"):
        return "percent"
    if kind == "index":
        return "index"
    return source_unit  # 3ma


def _minus_one_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year - 1, day=28)


def _minus_one_month(d: date) -> date:
    year = d.year - 1 if d.month == 1 else d.year
    month = 12 if d.month == 1 else d.month - 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def _dims_key(dims) -> str:
    return json.dumps(dims or {}, sort_keys=True)


def _pct(current, base) -> float | None:
    if current is None or base is None or base == 0:
        return None
    return (current / base - 1.0) * 100.0


def validate(kind: str, frequency: str) -> None:
    """Reject a transform that can't sensibly apply to this frequency."""
    if kind not in TRANSFORMS:
        raise TransformError(
            f"unknown transform {kind!r}; choose one of {', '.join(sorted(TRANSFORMS))}"
        )
    freq = (frequency or "").lower()
    if kind == "yoy" and freq not in _YOY_OK:
        raise TransformError(
            f"transform 'yoy' needs a sub-annual series; this one is {frequency!r} "
            f"— use 'pct_change' instead"
        )
    if kind == "mom" and freq not in _MOM_OK:
        raise TransformError(
            f"transform 'mom' needs a monthly series; this one is {frequency!r} "
            f"— use 'pct_change' or 'yoy' instead"
        )


def _apply_group(rows_asc: list[dict], kind: str, frequency: str) -> list[dict]:
    """Apply a transform to one dims-group, given ascending-by-date rows."""
    out: list[dict] = []
    freq = (frequency or "").lower()

    if kind in ("yoy", "mom"):
        by_date = {r["obs_date"]: r["value"] for r in rows_asc}
        dates_sorted = [r["obs_date"] for r in rows_asc]  # already ascending
        shift = _minus_one_year if kind == "yoy" else _minus_one_month
        for r in rows_asc:
            target = shift(r["obs_date"])
            base = by_date.get(target)
            if base is None and kind == "yoy" and freq == "daily":
                base = _nearest_prior(dates_sorted, by_date, target, _YOY_TOLERANCE_DAYS)
            out.append({**r, "value": _pct(r["value"], base)})
        return out

    if kind == "pct_change":
        prev = None
        for r in rows_asc:
            out.append({**r, "value": _pct(r["value"], prev)})
            prev = r["value"]
        return out

    if kind == "3ma":
        window: list[float] = []
        for r in rows_asc:
            v = r["value"]
            if v is None:
                window = []  # a gap breaks the trailing window
                out.append({**r, "value": None})
                continue
            window.append(v)
            if len(window) > 3:
                window.pop(0)
            out.append({**r, "value": sum(window) / 3 if len(window) == 3 else None})
        return out

    # index — rebase to 100 at the first non-null value in the window
    base = next((r["value"] for r in rows_asc if r["value"] is not None), None)
    for r in rows_asc:
        v = r["value"]
        out.append({**r, "value": (v / base * 100.0) if (v is not None and base) else None})
    return out


def _nearest_prior(dates_sorted, by_date, target, tolerance_days):
    """Closest date at or before `target` within tolerance (for daily yoy)."""
    best = None
    for d in dates_sorted:
        if d > target:
            break
        if target - d <= timedelta(days=tolerance_days):
            best = d
    return by_date.get(best) if best is not None else None


def apply_transform(rows: list[dict], kind: str, frequency: str) -> list[dict]:
    """Apply `kind` to `rows` (any order), grouped by dims. Returns rows in
    ascending date order; the caller re-sorts to the requested direction.

    Each row must have `obs_date`, `value` (float|None) and `dims`.
    """
    validate(kind, frequency)
    # DB numerics arrive as decimal.Decimal; coerce to float so transform math
    # never mixes Decimal and float (and the output matches the JSON envelope).
    groups: dict[str, list[dict]] = {}
    for r in rows:
        value = r["value"]
        norm = {**r, "value": float(value) if value is not None else None}
        groups.setdefault(_dims_key(r.get("dims")), []).append(norm)
    result: list[dict] = []
    for grp in groups.values():
        grp_asc = sorted(grp, key=lambda r: r["obs_date"])
        result.extend(_apply_group(grp_asc, kind, frequency))
    result.sort(key=lambda r: r["obs_date"])
    return result
