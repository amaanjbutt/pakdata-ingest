"""discover_easydata — expand the EasyData catalog by harvesting series metadata.

EasyData has no "list all datasets" endpoint. Series keys must be discovered in
two steps:

  1. Harvest dataset codes (e.g. TS_GP_BOP_WR_M) from the APEX portal navigation
     (https://easydata.sbp.org.pk/apex/f?p=10:1 — the P211_DATASET_TYPE_CODE
     values in the tree URLs). That is a one-time browser task (by hand or a
     Playwright crawler) and is OUT OF SCOPE here — this script consumes whatever
     codes are listed in ingestion/dataset_codes.json.
  2. For each dataset code, call GET /api/v1/dataset/{code}/meta which returns one
     row per series key, with columns: Reference Dataset Name, Series Code,
     Series Name, Series Description, Data Frequency, Unit, Variable Type,
     Available Since, Available Upto, Last Refresh Date.

This script writes/updates ingestion/easydata_series.json with one generated
entry per discovered series. It is idempotent: existing entries (including the
hand-curated ones) win — a discovered series whose easydata_key is already
present is skipped, so curated names/bounds are never clobbered.

Usage:  EASYDATA_API_KEY=... python -m ingestion.discover_easydata [--limit N]

Respects the API rate limits (250/hr, 2000/day) by spacing calls; a full harvest
is a handful of dataset-meta calls, well under the cap.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime

import httpx

from app.config import settings
from ingestion.quota import QuotaCounter

DATASET_META_URL = "https://easydata.sbp.org.pk/api/v1/dataset/{code}/meta"
CALL_SPACING_SECONDS = 0.4

_HERE = os.path.dirname(__file__)
DATASET_CODES_PATH = os.path.join(_HERE, "dataset_codes.json")
SERIES_JSON_PATH = os.path.join(_HERE, "easydata_series.json")

_FREQUENCY_MAP = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "annual": "annual",
    "annually": "annual",
    "yearly": "annual",
    "half-yearly": "half_yearly",
    "semester": "half_yearly",
    "as-needed": "irregular",
    "as needed": "irregular",
    "occasionally annual": "irregular",
}

_DATE_FORMATS = ["%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%b-%Y", "%Y-%m", "%Y"]


# ---- pure helpers (unit/frequency/id/date mapping) --------------------------

def strip_html(text: str | None) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def map_unit(raw_unit: str | None, series_name: str | None) -> str:
    u = (raw_unit or "").strip().lower()
    name = (series_name or "").lower()
    if not u:
        return ""
    millions = "million" in u or "mn" in u.split() or "million" in name
    if "usd" in u or "dollar" in u:
        return "usd_mn" if millions else "usd"
    if "pkr" in u or "rupee" in u or u.startswith("rs"):
        return "pkr_mn" if millions else "pkr"
    if u in ("number", "count", "no.", "nos", "numbers"):
        return "count"
    if "percent" in u or u == "%":
        return "percent"
    if "index" in u:
        return "index"
    return u  # keep raw (lowercased) as a fallback


def map_frequency(raw: str | None) -> str:
    key = (raw or "").strip().lower()
    if not key:
        return ""
    
    # Split by comma or slash and match parts
    parts = [p.strip() for p in re.split(r"[,/]", key) if p.strip()]
    
    def norm(s: str) -> str:
        return s.replace("-", "").replace("_", "").replace(" ", "")

    norm_map = {
        norm(k): v for k, v in _FREQUENCY_MAP.items()
    }
    norm_map["halfyearly"] = "half_yearly"

    for part in parts:
        norm_part = norm(part)
        if norm_part in norm_map:
            return norm_map[norm_part]
            
    norm_whole = norm(key)
    if norm_whole in norm_map:
        return norm_map[norm_whole]
        
    return key


def to_iso_date(raw: str | None) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _dataset_slug(dataset_code: str) -> str:
    s = dataset_code.lower()
    if s.startswith("ts_"):
        s = s[3:]
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def _series_suffix(series_code: str) -> str:
    suffix = series_code.split(".")[-1]
    return re.sub(r"[^a-z0-9]+", "_", suffix.lower()).strip("_")


# Clean-id topic scheme (kept in sync with scripts/generate_id_map.py). Existing
# series keep their ids via append-only merge; this only shapes NEW series.
_DATASET_TOPIC = {
    "PSAUTO": "auto", "ELECGEN": "power", "POLSALE": "fuel", "SALEFERT": "agriculture",
    "QGDP1516": "gdp", "PAKGDP15": "gdp", "WR": "remittances", "PAKRES": "reserves",
    "FAERPKR": "fx.rate.avg", "FMEERPKR": "fx.rate.monthend", "REERNEER": "fx.effective",
    "KSORDA": "roshan_digital", "EPUI": "sentiment", "PAKPOP": "population",
    "BBTD": "payments.branchless", "SGADVNPL": "banking.npl",
}
_SUBJECT_TOPIC = {
    "BOP": "bop", "ES": "external", "FI": "investment", "ED": "debt.external",
    "PKDP": "debt", "PDL": "debt", "ER": "fx", "BAM": "banking", "MFS": "money",
    "BS": "banking", "FSA": "corporate", "RLS": "industry", "RS": "gdp", "GA": "gdp",
    "RL": "social", "PT": "inflation", "PF": "fiscal", "IR": "rates", "EXT": "external",
}
_STOPWORDS = {"of", "the", "in", "and", "by", "to", "for", "a", "an", "at", "on",
              "per", "from", "s", "pak", "pakistan", "all"}


def _topic_for(dataset_code: str) -> str:
    parts = dataset_code.split("_")
    subj = parts[2] if len(parts) > 2 else ""
    dset = parts[3] if len(parts) > 3 else ""
    return _DATASET_TOPIC.get(dset) or _SUBJECT_TOPIC.get(subj) or "economic"


def _name_slug(name: str) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return "_".join(w for w in words if w not in _STOPWORDS)[:44].strip("_")


def derive_id(dataset_code: str, series_code: str, name: str = "") -> str:
    """Clean, URL-friendly catalog id: '<topic>.<name-slug>'. Falls back to the
    dataset/series slug when no name is available."""
    leaf = _name_slug(name) or _series_suffix(series_code)
    return f"{_topic_for(dataset_code)}.{leaf}"


def _find_col(columns: list[str], *names: str) -> int:
    low = [c.strip().lower() for c in columns]
    for n in names:  # exact match first
        nl = n.lower()
        for i, c in enumerate(low):
            if c == nl:
                return i
    for n in names:  # then substring
        nl = n.lower()
        for i, c in enumerate(low):
            if nl in c:
                return i
    raise ValueError(f"dataset/meta response missing any of {names}; got {columns}")


# SBP's "Reference Dataset Name" often carries publication scaffolding we don't
# want in a display label ("... - Summary", "of Pakistan" — everything is Pakistan).
_DATASET_LABEL_STRIPS = (" - Summary", " - Detail", " - Details", " - Overall", " (Summary)")


def clean_dataset_label(raw: str | None) -> str:
    """A short, human dataset context from EasyData's 'Reference Dataset Name'."""
    s = re.sub(r"<[^>]+>", "", raw or "").strip()
    if not s:
        return ""
    for suf in _DATASET_LABEL_STRIPS:
        if s.endswith(suf):
            s = s[: -len(suf)].strip()
    s = re.sub(r"\s+(?:of|in|for)\s+Pakistan\b", "", s, flags=re.I).strip()
    return re.sub(r"\s{2,}", " ", s)


def compose_name(leaf: str | None, dataset_label: str | None) -> str:
    """Self-describing catalog name: the specific leaf first, dataset context
    after — so a bare 'Between fellow enterprises' reads as
    'Between fellow enterprises — International Investment Position (BPM6)'.
    Falls back cleanly when either piece is missing or already redundant."""
    leaf = re.sub(r"\s{2,}", " ", (leaf or "").strip())
    ds = clean_dataset_label(dataset_label)
    if not ds:
        return leaf
    if not leaf:
        return ds
    if ds.lower() in leaf.lower() or leaf.lower() in ds.lower():
        return leaf
    return f"{leaf} — {ds}"


def compose_description(raw_desc: str | None, leaf: str | None, dataset_label: str | None) -> str:
    """Prefer SBP's own description; when blank (common for BOP/IIP), synthesise
    context from the leaf + dataset so the page isn't left unexplained."""
    d = re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", "", raw_desc or "").strip())
    if d:
        return d
    ds = clean_dataset_label(dataset_label)
    leaf = (leaf or "").strip()
    if ds and leaf:
        return f"{leaf}, from the State Bank of Pakistan’s “{ds}” dataset."
    return d


def parse_dataset_meta(text: str, dataset_code: str) -> list[dict]:
    """Turn a dataset/meta JSON payload into catalog entry dicts. Pure — no IO."""
    payload = json.loads(text, strict=False)
    columns = payload["columns"]
    rows = payload.get("rows", [])

    c_code = _find_col(columns, "Series Code", "Series Key")
    c_name = _find_col(columns, "Series Name")
    c_desc = _find_col(columns, "Series Description", "Description")
    c_freq = _find_col(columns, "Data Frequency", "Frequency")
    c_unit = _find_col(columns, "Unit")
    c_since = _find_col(columns, "Available Since", "Available From")
    try:
        c_refdataset = _find_col(columns, "Reference Dataset Name", "Dataset Name")
    except ValueError:
        c_refdataset = None
    try:
        c_refresh = _find_col(columns, "Last Refresh Date", "Last Refresh")
    except ValueError:
        c_refresh = None

    entries: list[dict] = []
    for row in rows:
        series_code = str(row[c_code]).strip()
        if not series_code:
            continue
        leaf = str(row[c_name]).strip()
        dataset_label = str(row[c_refdataset]).strip() if c_refdataset is not None else ""
        entries.append(
            {
                "easydata_key": series_code,
                # id derives from the raw LEAF (unchanged) so existing ids stay stable.
                "id": derive_id(dataset_code, series_code, leaf),
                "module": "economic",
                "name": compose_name(leaf, dataset_label),
                "description": compose_description(row[c_desc], leaf, dataset_label),
                "reference_dataset": clean_dataset_label(dataset_label),
                "unit": map_unit(row[c_unit], leaf),
                "frequency": map_frequency(row[c_freq]),
                "source": "SBP",
                "tier": "basic",
                "min_value": None,
                "max_value": None,
                "max_step": None,
                "backfill_start": to_iso_date(row[c_since]) or "2000-01-01",
                "easydata_dataset_code": dataset_code,
                "easydata_last_refresh": to_iso_date(row[c_refresh]) if c_refresh is not None else None,
            }
        )
    return entries


def merge_entries(existing: list[dict], discovered: list[dict]) -> tuple[list[dict], int]:
    """Merge discovered entries into existing. Existing entries win by
    easydata_key (curated series are never clobbered). Ids are kept unique by
    suffixing a counter on collision. Returns (merged, num_added)."""
    by_key = {e["easydata_key"]: e for e in existing}
    used_ids = {e["id"] for e in existing}
    merged = list(existing)
    added = 0
    for entry in discovered:
        if entry["easydata_key"] in by_key:
            continue  # existing / curated wins
        eid = entry["id"]
        if eid in used_ids:
            n = 2
            while f"{eid}_{n}" in used_ids:
                n += 1
            entry = {**entry, "id": f"{eid}_{n}"}
        used_ids.add(entry["id"])
        by_key[entry["easydata_key"]] = entry
        merged.append(entry)
        added += 1
    return merged, added


# ---- IO / orchestration -----------------------------------------------------

def _load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _redact(text: str, api_key: str | None) -> str:
    return text.replace(api_key, "***REDACTED***") if api_key else text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingestion.discover_easydata")
    parser.add_argument("--limit", type=int, default=None, help="max dataset codes to process")
    args = parser.parse_args(argv)

    api_key = os.getenv("EASYDATA_API_KEY")
    if not api_key:
        raise SystemExit("EASYDATA_API_KEY is not set")

    codes = _load_json(DATASET_CODES_PATH).get("dataset_codes", [])
    if args.limit:
        codes = codes[: args.limit]

    config = _load_json(SERIES_JSON_PATH)
    existing = config.get("series", [])

    quota = QuotaCounter(os.getenv("EASYDATA_QUOTA_PATH", "./data/easydata_quota.json"))
    discovered: list[dict] = []
    for i, code in enumerate(codes):
        if not quota.allow():
            print("[Warning] Quota limit reached/exhausted. Stopping discovery early.")
            break
        if i:
            time.sleep(CALL_SPACING_SECONDS)
        quota.record()
        url = DATASET_META_URL.format(code=code) + f"?api_key={api_key}&format=json"
        try:
            resp = httpx.get(url, headers={"User-Agent": settings.user_agent}, timeout=30)
            resp.raise_for_status()
            entries = parse_dataset_meta(resp.text, code)
            discovered.extend(entries)
            print(f"{code}: {len(entries)} series")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                print(f"[Warning] dataset/meta returned 404 for {code}. Skipping.")
                continue
            else:
                print(f"[Warning] dataset/meta failed with status {exc.response.status_code} for {code}: {_redact(str(exc), api_key)}. Skipping.")
                continue
        except Exception as exc:
            print(f"[Warning] dataset/meta failed to process/parse for {code}: {_redact(str(exc), api_key)}. Skipping.")
            continue

    merged, added = merge_entries(existing, discovered)
    if len(merged) < len(existing):
        raise ValueError(
            f"Catalog shrink detected! Attempted to write {len(merged)} series (existing: {len(existing)}). Aborting write."
        )
    config["series"] = merged
    with open(SERIES_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"discovered {len(discovered)} series across {len(codes)} datasets; "
          f"added {added} new (total now {len(merged)}).")
    print("Next: python -m db.init  &&  python -m ingestion.run easydata_sync --backfill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
