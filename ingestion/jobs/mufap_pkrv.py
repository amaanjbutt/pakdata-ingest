"""mufap_pkrv — MUFAP rupee yield curves (PKRV conventional + PKISRV Islamic), daily.

Source: MUFAP publishes daily PKRV sheets under the Pricing section. The page is
a JS app, but the underlying data API is a plain POST that returns the full file
list with direct download URLs:

    POST https://www.mufap.com.pk/WebRegulations/GetSecpFileById
         {"fk_HeaderSubMenuTabId": 46}
    -> {"data": [{"Title": "PKRV22072026", "Date": "/Date(ms)/",
                  "FilePath": "/Upload/WebDoc/IndustryStatictics/PKRV...csv"}, ...]}

Recent daily files are small CSVs:

    Tenor,Mid Rate,Change
    1W,11.52,0.00
    ...
    20Y,12.39,0.05

We ingest the Mid Rate per tenor as `pkrv.<tenor>` (e.g. pkrv.3m, pkrv.10y). The
observation date comes from the file Title (PKRV + DDMMYYYY). Older months are
published as .xlsx and are skipped here (daily CSVs cover 2022-02 onward);
xlsx backfill can be added later.

Licensing: PKRV are FMAP/MUFAP benchmark rates that can carry redistribution
terms. Per confirmation from the operator, these series are published active.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime

from app.config import settings
from ingestion.framework import FetchedFile, IngestionJob, Record

LIST_URL = "https://www.mufap.com.pk/WebRegulations/GetSecpFileById"
FILE_BASE = "https://www.mufap.com.pk"
PKRV_TAB_ID = 46
INCREMENTAL_FILES = 5  # most recent N daily files on an incremental run

# Canonical PKRV tenor order (mirrors the CSV rows).
TENORS = ["1w", "2w", "1m", "2m", "3m", "4m", "6m", "9m", "1y", "2y", "3y",
          "4y", "5y", "6y", "7y", "8y", "9y", "10y", "15y", "20y"]
# PKISRV (Islamic) publishes a shorter benchmark curve.
PKISRV_TENORS = ["1m", "3m", "6m", "9m", "1y"]


def _norm_tenor(text: str) -> str | None:
    """'1 - Month' / '10Y' / '6-Month' -> '1m' / '10y' / '6m'."""
    m = re.search(r"(\d+)\s*[-\s]?\s*(week|month|year|w|m|y)", text.strip().lower())
    if not m:
        return None
    unit = {"week": "w", "month": "m", "year": "y"}.get(m.group(2), m.group(2))
    return f"{m.group(1)}{unit}"


def parse_pkrv_csv(text: str, obs_date: date) -> list[Record]:
    """Parse a daily PKRV CSV (Tenor,Mid Rate,Change) into per-tenor records. Pure."""
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    # Locate the 'Tenor' header row — older issues carry preamble/title lines
    # above it rather than starting with the header.
    header_idx = None
    for i, row in enumerate(rows[:10]):
        if row and "tenor" in (row[0] or "").strip().lower():
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("not a PKRV CSV (missing Tenor header)")
    records: list[Record] = []
    for row in rows[header_idx + 1:]:
        if len(row) < 2:
            continue
        tenor = _norm_tenor(row[0])
        if tenor is None or not re.match(r"^\d+[wmy]$", tenor):
            continue
        try:
            mid = float(str(row[1]).strip())
        except ValueError:
            continue
        records.append(Record(f"pkrv.{tenor}", obs_date, mid, {}))
    if not records:
        raise ValueError("no tenor rows parsed from PKRV CSV")
    return records


def parse_pkisrv_csv(text: str, obs_date: date) -> list[Record]:
    """Parse the PKISRV (Islamic) daily CSV. The Shariah yield curve is embedded
    in the right-hand columns under a 'Tenor | PKISRV Rates (Yields)' header
    (the left columns carry per-sukuk instrument prices, ignored here). Pure."""
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    # Locate the curve's header cell to learn which two columns hold tenor+yield.
    tcol = ycol = None
    for row in rows:
        for i, cell in enumerate(row):
            if cell.strip().lower() == "tenor" and i + 1 < len(row):
                tcol, ycol = i, i + 1
                break
        if tcol is not None:
            break
    if tcol is None:
        raise ValueError("PKISRV curve header not found")
    records: list[Record] = []
    for row in rows:
        if len(row) <= ycol:
            continue
        tenor = _norm_tenor(row[tcol])
        if tenor is None or not re.match(r"^\d+[wmy]$", tenor):
            continue
        m = re.search(r"-?\d+(?:\.\d+)?", row[ycol])
        if not m:
            continue
        records.append(Record(f"pkisrv.{tenor}", obs_date, float(m.group(0)), {}))
    if not records:
        raise ValueError("no tenor rows parsed from PKISRV CSV")
    return records


# Curves ingested by this job: (title prefix, series prefix, parser).
_CURVES = [
    ("PKRV", "pkrv", parse_pkrv_csv),
    ("PKISRV", "pkisrv", parse_pkisrv_csv),
]


class MufapPkrvJob(IngestionJob):
    name = "mufap_pkrv"
    source = "MUFAP"

    def _all_files(self) -> list[dict]:
        resp = self._post_json(LIST_URL, {"fk_HeaderSubMenuTabId": PKRV_TAB_ID})
        return json.loads(resp)["data"]

    def _curve_files(self, items: list[dict], prefix: str) -> list[tuple[date, str]]:
        """[(obs_date, file_url)] for a curve's daily CSVs, newest first."""
        rx = re.compile(rf"^{prefix}(\d{{8}})$")
        out: list[tuple[date, str]] = []
        for item in items:
            title = str(item.get("Title", "")).strip()
            path = str(item.get("FilePath", ""))
            m = rx.match(title)
            if not m or not path.lower().endswith(".csv"):
                continue
            try:
                d = datetime.strptime(m.group(1), "%d%m%Y").date()
            except ValueError:
                continue
            out.append((d, FILE_BASE + path))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def _post_json(self, url: str, body: dict) -> str:
        from ingestion import http_client

        return http_client.post(url, json=body, timeout=45).text

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        items = self._all_files()
        out: list[FetchedFile] = []
        for prefix, _sp, _parser in _CURVES:
            files = self._curve_files(items, prefix)
            if not backfill:
                files = files[:INCREMENTAL_FILES]
            for d, url in files:
                try:
                    content = self.http_get(url)
                except Exception:
                    continue
                out.append(FetchedFile(filename=f"{prefix}{d:%d%m%Y}.csv", content=content, when=d))
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        text = f.content.decode("utf-8", errors="replace")
        # Dispatch by filename prefix — longest prefix first so PKISRV beats PKRV.
        for prefix, _sp, parser in sorted(_CURVES, key=lambda c: -len(c[0])):
            if f.filename.upper().startswith(prefix):
                return parser(text, f.when)
        raise ValueError(f"unknown MUFAP curve file: {f.filename}")
