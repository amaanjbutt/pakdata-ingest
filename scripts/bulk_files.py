"""Whole-dataset files: every series of a dataset in one file, for the Data pass, Pro and Business.

A dataset = every active series sharing the first part of its id (`rates.*` = KIBOR, policy rate
and the rest of the interest rates; `remittances.*` = every corridor; `bop.*` = the balance of
payments). For each, a gzipped CSV and — up to a million rows — an Excel workbook go to the R2
bucket (R2_BULK_BUCKET) under stable keys; `bulk_files` records them and GET /v1/bulk signs links.
A dataset whose rows haven't changed since the last build (same count, same newest revision
time) is skipped, so the nightly run only rewrites what moved.

    python -m scripts.bulk_files [--force] [--only rates,fx] [--dry-run]

Scheduled nightly at 03:20 PKT on the VPS (ingestion/scheduler.py).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import logging
import time
from datetime import date

from app import db
from app.services import r2

log = logging.getLogger("pakdata.bulk")

XLSX_MAX_ROWS = 1_000_000

# Readable names for the dataset prefixes (anything missing falls back to the prefix).
LABELS = {
    "agriculture": "Agriculture (cotton arrivals and crops)", "auto": "Car, bike and truck sales",
    "banking": "Banking (deposits, advances, banks' balance sheets)", "bop": "Balance of payments",
    "commodities": "Commodity prices", "corporate": "Corporate sector", "cost_of_living": "Cost of living by city",
    "debt": "Public debt", "education": "Education", "energy": "Energy (fuel, power, merit order)",
    "external": "External sector (FDI, portfolio flows, external debt)", "fiscal": "Fiscal operations",
    "fuel": "Fuel prices", "funds": "Mutual fund industry", "gdp": "GDP and national accounts",
    "health": "Health", "industry": "Industry and manufacturing (LSM)", "inflation": "Inflation (CPI, SPI, WPI)",
    "investment": "Investment (FDI by country and sector)", "money": "Money and credit",
    "payments": "Payment systems (Raast, cards, digital)", "population": "Population",
    "ports": "Ports and shipping", "power": "Electricity", "prices": "Prices", "rates": "Interest rates (KIBOR, policy rate, yields)",
    "regional": "Districts and provinces", "remittances": "Workers' remittances by country",
    "reserves": "Foreign exchange reserves", "roshan_digital": "Roshan Digital Accounts",
    "sentiment": "Consumer and business confidence", "sme": "SME finance", "social": "Social indicators",
    "telecom": "Telecom (subscribers, broadband)", "trade": "External trade", "water": "River flows and dams",
    "fx": "Exchange rates",
}


def label(dataset: str) -> str:
    return LABELS.get(dataset, dataset.replace("_", " ").capitalize())


def datasets() -> list[dict]:
    """Every dataset with its fingerprint (rows, series, newest revision time)."""
    return db.query(
        """SELECT split_part(s.id, '.', 1) AS dataset, count(DISTINCT s.id) AS series_count,
                  count(*) AS rows, max(o.revised_at) AS revised
           FROM series s JOIN observations o ON o.series_id = s.id
           WHERE s.is_active GROUP BY 1 ORDER BY 1""")


def _fingerprint(d: dict) -> str:
    return f"{d['rows']}:{d['series_count']}:{d['revised'].isoformat() if d['revised'] else ''}"


def _rows(dataset: str):
    """Stream the dataset's observations (server-side cursor): series metadata + one point each."""
    with db.connection() as conn:
        with conn.cursor(name=f"bulk_{dataset}") as cur:
            cur.itersize = 20_000
            cur.execute(
                """SELECT s.id, s.name, s.unit, s.frequency,
                          CASE WHEN s.derivation IS NOT NULL THEN 'PakDataHub (derived)' ELSE s.source END,
                          o.obs_date, o.value, o.dims
                   FROM series s JOIN observations o ON o.series_id = s.id
                   WHERE s.is_active AND split_part(s.id, '.', 1) = %s
                   ORDER BY s.id, o.obs_date, o.dims::text""", (dataset,))
            yield from cur


HEADER = ["series_id", "series_name", "unit", "frequency", "source", "date", "value", "dims"]


def _citation(dataset: str) -> str:
    return (f"{label(dataset)} — every series, full history. Source: the publishers named per row, via PakDataHub "
            f"(https://pakdatahub.com). Built {date.today().isoformat()}. Please credit PakDataHub with a link.")


def _value(v) -> str:
    if v is None:
        return ""
    try:
        return format(v.normalize(), "f")  # Decimal: 12.5000 -> 12.5, 1E+2 -> 100
    except AttributeError:
        return repr(float(v))


def build_csv(dataset: str) -> tuple[bytes, int]:
    raw = io.BytesIO()
    n = 0
    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as gz:
        text = io.TextIOWrapper(gz, encoding="utf-8", newline="")
        text.write("# " + _citation(dataset) + "\n")
        w = csv.writer(text, lineterminator="\n")
        w.writerow(HEADER)
        for sid, name, unit, freq, source, d, v, dims in _rows(dataset):
            w.writerow([sid, name, unit or "", freq, source, d.isoformat(), _value(v),
                        json.dumps(dims, separators=(",", ":"), sort_keys=True) if dims else ""])
            n += 1
        text.flush()
        text.detach()
    return raw.getvalue(), n


def build_xlsx(dataset: str) -> bytes:
    from openpyxl import Workbook

    wb = Workbook(write_only=True)
    data = wb.create_sheet("Data")
    data.append(HEADER)
    meta: dict[str, list] = {}
    for sid, name, unit, freq, source, d, v, dims in _rows(dataset):
        data.append([sid, name, unit or "", freq, source, d, None if v is None else float(v),
                     json.dumps(dims, separators=(",", ":"), sort_keys=True) if dims else ""])
        m = meta.setdefault(sid, [sid, name, unit or "", freq, source, d, d, 0])
        m[6] = d
        m[7] += 1
    series = wb.create_sheet("Series")
    series.append(["series_id", "series_name", "unit", "frequency", "source", "first_date", "last_date", "rows",
                   "page"])
    for m in meta.values():
        series.append([*m, f"https://pakdatahub.com/series/{m[0]}"])
    about = wb.create_sheet("About")
    about.append(["Dataset", label(dataset)])
    about.append(["Cite as", _citation(dataset)])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def run(force: bool = False, only: list[str] | None = None, dry_run: bool = False) -> dict:
    if not r2.configured() and not dry_run:
        raise RuntimeError("R2 is not configured (R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
    have = {(r["dataset"], r["format"]): r for r in db.query("SELECT * FROM bulk_files")}
    built, skipped = [], []
    for d in datasets():
        ds = d["dataset"]
        if only and ds not in only:
            continue
        fp = _fingerprint(d)
        prev = have.get((ds, "csv.gz"))
        if not force and prev and prev.get("fingerprint") == fp:
            skipped.append(ds)
            continue
        t0 = time.time()
        body, n = build_csv(ds)
        files = [("csv.gz", body, "application/gzip")]
        if n <= XLSX_MAX_ROWS:
            files.append(("xlsx", build_xlsx(ds),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        for fmt, blob, ctype in files:
            key = f"bulk/{ds}/pakdatahub_{ds}.{fmt}"
            if dry_run:
                log.info("bulk %s.%s: %d rows, %d bytes (dry run)", ds, fmt, n, len(blob))
                continue
            r2.put_object(key, blob, ctype)
            db.execute(
                """INSERT INTO bulk_files (dataset, format, object_key, bytes, series_count, rows, built_at, fingerprint)
                   VALUES (%s, %s, %s, %s, %s, %s, now(), %s)
                   ON CONFLICT (dataset, format) DO UPDATE SET object_key = EXCLUDED.object_key,
                     bytes = EXCLUDED.bytes, series_count = EXCLUDED.series_count, rows = EXCLUDED.rows,
                     built_at = now(), fingerprint = EXCLUDED.fingerprint""",
                (ds, fmt, key, len(blob), d["series_count"], n, fp))
        if n > XLSX_MAX_ROWS and not dry_run:
            db.execute("DELETE FROM bulk_files WHERE dataset = %s AND format = 'xlsx'", (ds,))
        built.append(ds)
        log.info("bulk %s: %d rows in %.1fs", ds, n, time.time() - t0)
    # datasets that no longer exist (every series deactivated) leave the listing
    live = {d["dataset"] for d in datasets()} if not only else None
    if live is not None and not dry_run:
        db.execute("DELETE FROM bulk_files WHERE NOT (dataset = ANY(%s))", (list(live),))
    log.info("bulk_files: built %d, unchanged %d", len(built), len(skipped))
    return {"built": built, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="rebuild every dataset, changed or not")
    ap.add_argument("--only", default="", help="comma-separated dataset prefixes")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run(force=a.force, only=[s for s in a.only.split(",") if s] or None, dry_run=a.dry_run)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
