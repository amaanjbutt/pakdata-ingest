"""JSON rows → CSV, for `?format=csv` on every list endpoint (app/main.py middleware).

Nested objects flatten to dotted columns (`latest_nav.nav`), lists to compact JSON. Cells that a
spreadsheet would run as a formula (= + - @ tab CR) are prefixed with a quote — except numbers.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any


def _flatten(row: dict, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    for k, v in row.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            if v:
                _flatten(v, key + ".", out)
            else:
                out[key] = ""
        elif isinstance(v, list):
            out[key] = json.dumps(v, separators=(",", ":"), ensure_ascii=False) if v else ""
        else:
            out[key] = v
    return out


def _safe(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        try:
            float(s)
            return s
        except ValueError:
            return "'" + s
    return s


def rows_to_csv(rows: list[dict]) -> str:
    flat = [_flatten(r) if isinstance(r, dict) else {"value": r} for r in rows]
    cols: list[str] = []
    seen: set[str] = set()
    for r in flat:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in flat:
        w.writerow([_safe(r.get(c)) for c in cols])
    return buf.getvalue()
