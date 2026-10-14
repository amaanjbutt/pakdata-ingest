"""dq_audit — find suspect values and confirm them against the publisher (2026-10-14).

`data_quality` (nightly) only auto-fixes a few unambiguous glitches. This script is the
audit behind it: it flags everything that *looks* wrong, then settles each case by
re-reading the official source, so a value is either confirmed as published, corrected,
or reported as unverifiable — never left at "suspicious".

  python -m scripts.dq_audit detect [--commit]            # VPS, api container: run the detectors
  python -m scripts.dq_audit reconcile-easydata [--budget N] [--all] [--apply] [--commit]
                                                          # GHA (SBP blocks the VPS): every stored
                                                          # point of each SBP EasyData series vs
                                                          # EasyData's current figures
  python -m scripts.dq_audit compare-records <method> records.json [--commit]
                                                          # records parsed from the publisher's files (job
                                                          # parser, on a PC that reaches the source) vs DB
  python -m scripts.dq_audit resolve-copies [--commit]    # rates.kibor.* / rates.auction.* findings via
                                                          # their (reconciled) EasyData inputs
  python -m scripts.dq_audit mark verdicts.json [--commit] # verdicts read off the source document by hand
  python -m scripts.dq_audit report [out.csv]             # summary + CSV of findings/mismatches

Tables (created on first use, also in db/schema.sql):
  dq_findings     one row per (series, date, dims, detector) — what looked wrong, its tier and,
                  once checked, the verdict + the source's value
  dq_series_checks one row per series per method — the last whole-series reconciliation
  dq_mismatches   every point where our value and the source's disagree (or one side lacks it)

Verdicts: confirmed_as_published · source_differs (we hold a value the publisher no longer
shows — a revision we missed, or our error) · not_in_source · scale_corrected (our nightly
unit-slip fix, deliberately different from the source).
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

log = logging.getLogger("pakdata.dq_audit")

DDL = [
    """CREATE TABLE IF NOT EXISTS dq_findings (
         id BIGSERIAL PRIMARY KEY, series_id TEXT NOT NULL, obs_date DATE NOT NULL,
         dims JSONB NOT NULL DEFAULT '{}', detector TEXT NOT NULL, tier SMALLINT NOT NULL,
         our_value NUMERIC, context JSONB, verdict TEXT, source_value NUMERIC, evidence TEXT,
         checked_at TIMESTAMPTZ, found_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE (series_id, obs_date, dims, detector))""",
    """CREATE TABLE IF NOT EXISTS dq_series_checks (
         series_id TEXT NOT NULL, method TEXT NOT NULL, checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         source_points INT, our_points INT, matched INT, mismatched INT, missing_ours INT,
         missing_source INT, error TEXT, PRIMARY KEY (series_id, method))""",
    """CREATE TABLE IF NOT EXISTS dq_mismatches (
         series_id TEXT NOT NULL, obs_date DATE NOT NULL, dims JSONB NOT NULL DEFAULT '{}',
         method TEXT NOT NULL, kind TEXT NOT NULL, our_value NUMERIC, source_value NUMERIC,
         checked_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (series_id, obs_date, dims, method))""",
]

# Tier: 1 = curated/scraped/derived series (what the site and tools feature), 3 = the SBP
# EasyData long tail. (Dedicated tables — funds, mandi, securities — are tier 2.)
_TIER = "CASE WHEN s.easydata_dataset_code IS NULL THEN 1 ELSE 3 END"

# Each detector returns (series_id, obs_date, dims, our_value, context).
_NEIGHBOURS = """
  SELECT o.series_id, o.obs_date, o.dims, o.value::float8 AS v,
         lag(o.value::float8) OVER w AS pv, lead(o.value::float8) OVER w AS nv,
         s.unit, {tier} AS tier
  FROM observations o JOIN series s ON s.id = o.series_id
  WHERE s.is_active AND o.value IS NOT NULL
  WINDOW w AS (PARTITION BY o.series_id, o.dims ORDER BY o.obs_date)
""".format(tier=_TIER)

DETECTORS: dict[str, str] = {
    # A level that is >3x (or <1/3) both neighbours while they agree within 30%.
    "spike_level": f"""
      WITH n AS ({_NEIGHBOURS})
      SELECT series_id, obs_date, dims, v AS our_value, tier,
             jsonb_build_object('prev', pv, 'next', nv) AS context
      FROM n
      WHERE coalesce(unit, '') NOT IN ('percent') AND series_id NOT LIKE '%%app_dep%%'
        AND v <> 0 AND pv <> 0 AND nv <> 0 AND sign(v) = sign(pv) AND sign(pv) = sign(nv)
        AND abs(pv / nv - 1) < 0.3 AND greatest(abs(v / pv), abs(pv / v)) > 3""",
    # Rates/percentages: far from both neighbours relative to the series' own typical move
    # (median absolute month-to-month change), so ordinary MoM-percent noise isn't flagged.
    "spike_rate": f"""
      WITH n AS ({_NEIGHBOURS}),
      d AS (SELECT series_id, dims, percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(v - pv)) AS mad
            FROM n WHERE unit = 'percent' AND pv IS NOT NULL GROUP BY 1, 2)
      SELECT n.series_id, n.obs_date, n.dims, n.v AS our_value, n.tier,
             jsonb_build_object('prev', pv, 'next', nv, 'typical_move', d.mad) AS context
      FROM n JOIN d USING (series_id, dims)
      WHERE n.unit = 'percent' AND pv IS NOT NULL AND nv IS NOT NULL
        AND abs(pv - nv) <= greatest(2 * d.mad, 0.2)
        AND abs(v - (pv + nv) / 2) > greatest(8 * d.mad, 1.0)""",
    # Sign flip: neighbours agree, the point has the opposite sign and a similar magnitude.
    "sign_flip": f"""
      WITH n AS ({_NEIGHBOURS})
      SELECT series_id, obs_date, dims, v AS our_value, tier,
             jsonb_build_object('prev', pv, 'next', nv) AS context
      FROM n
      WHERE pv <> 0 AND nv <> 0 AND sign(pv) = sign(nv) AND sign(v) = -sign(pv)
        AND abs(pv / nv - 1) < 0.2 AND abs(abs(v) / abs(pv) - 1) < 0.2""",
    # Values the unit makes impossible.
    "out_of_range": f"""
      SELECT o.series_id, o.obs_date, o.dims, o.value::float8 AS our_value, {_TIER} AS tier,
             jsonb_build_object('unit', s.unit) AS context
      FROM observations o JOIN series s ON s.id = o.series_id
      WHERE s.is_active AND o.value IS NOT NULL AND (
            (s.unit = 'percent' AND (o.value > 1000 OR o.value < -100))
         OR (s.unit IN ('index', 'count', 'gwh', 'metric ton', 'tbps') AND o.value < 0)
         OR (s.unit LIKE 'pkr_per_%%' AND o.value <= 0))""",
    # A series and its canonical twin disagree by >0.5% — only twins with the same frequency
    # and unit (a monthly and a quarterly series sharing a label are different quantities;
    # those are a naming problem, listed by `report` as name collisions).
    "twin_mismatch": f"""
      SELECT a.series_id, a.obs_date, a.dims, a.value::float8 AS our_value, {_TIER} AS tier,
             jsonb_build_object('twin', s.canonical_id, 'twin_value', b.value::float8) AS context
      FROM series s
      JOIN series t ON t.id = s.canonical_id AND t.frequency = s.frequency AND t.unit IS NOT DISTINCT FROM s.unit
      JOIN observations a ON a.series_id = s.id
      JOIN observations b ON b.series_id = s.canonical_id AND b.obs_date = a.obs_date AND b.dims = a.dims
      WHERE s.is_active AND a.value IS NOT NULL AND b.value IS NOT NULL
        AND abs(a.value - b.value) > 0.005 * greatest(abs(a.value), abs(b.value))""",
    # Headline CPI YoY vs the YoY computed from the same index (1-dp rounding allows ~0.1pp).
    "cpi_yoy_vs_index": """
      WITH i AS (SELECT series_id, obs_date, value::float8 v FROM observations
                 WHERE series_id IN ('inflation.cpi.national','inflation.cpi.urban','inflation.cpi.rural','inflation.wpi')),
      c AS (SELECT a.series_id || '.yoy' AS sid, a.obs_date, (a.v / b.v - 1) * 100 AS calc
            FROM i a JOIN i b ON b.series_id = a.series_id
             AND b.obs_date = (date_trunc('month', a.obs_date) - interval '11 months' - interval '1 day')::date)
      SELECT o.series_id, o.obs_date, o.dims, o.value::float8 AS our_value, 1 AS tier,
             jsonb_build_object('yoy_from_index', round(c.calc::numeric, 2)) AS context
      FROM c JOIN observations o ON o.series_id = c.sid AND o.obs_date = c.obs_date
      WHERE abs(o.value - c.calc) > 0.25""",
    # Liquid reserves: total = SBP + banks.
    "reserves_identity": """
      SELECT t.series_id, t.obs_date, t.dims, t.value::float8 AS our_value, 1 AS tier,
             jsonb_build_object('sbp', s.value::float8, 'banks', b.value::float8) AS context
      FROM observations t
      JOIN observations s ON s.series_id = 'reserves.liquid.sbp' AND s.obs_date = t.obs_date
      JOIN observations b ON b.series_id = 'reserves.liquid.banks' AND b.obs_date = t.obs_date
      WHERE t.series_id = 'reserves.liquid.total' AND abs(t.value - s.value - b.value) > 0.15""",
    # Two-sided quotes: offer below bid.
    "bid_above_offer": """
      SELECT o.series_id, o.obs_date, o.dims, o.value::float8 AS our_value, 1 AS tier,
             jsonb_build_object('bid', b.value::float8) AS context
      FROM observations o JOIN observations b
        ON b.series_id = o.series_id AND b.obs_date = o.obs_date AND b.dims = '{"side": "bid"}'::jsonb
      WHERE o.dims = '{"side": "offer"}'::jsonb AND o.value < b.value""",
    # SPI: the national price is the mean of the cities' prices.
    "spi_national_vs_cities": """
      WITH c AS (SELECT series_id, obs_date, avg(value)::float8 m, count(*) n FROM observations
                 WHERE series_id LIKE 'commodities.%%' AND dims ? 'city' GROUP BY 1, 2)
      SELECT o.series_id, o.obs_date, o.dims, o.value::float8 AS our_value, 1 AS tier,
             jsonb_build_object('city_mean', round(c.m::numeric, 2), 'cities', c.n) AS context
      FROM observations o JOIN c USING (series_id, obs_date)
      WHERE o.series_id LIKE 'commodities.%%' AND o.dims = '{}'::jsonb AND c.n >= 10
        AND abs(o.value - c.m) > 0.02 * c.m""",
    # Six or more identical consecutive prints of a fast-moving price (a forward-fill bug).
    "flat_run": f"""
      WITH n AS (
        SELECT o.series_id, o.dims, o.obs_date, o.value,
               o.value IS NOT DISTINCT FROM lag(o.value) OVER w AS same, s.frequency, {_TIER} AS tier
        FROM observations o JOIN series s ON s.id = o.series_id
        WHERE s.is_active AND s.frequency IN ('daily', 'weekly', 'monthly')
          AND coalesce(s.unit, '') NOT IN ('percent', 'count') AND o.value IS NOT NULL AND o.value <> 0
          AND (s.id LIKE 'fx.rate.%%' OR s.id LIKE 'commodities.%%' OR s.id LIKE 'rates.pkrv.%%'
               OR s.id LIKE 'rates.pkisrv.%%' OR s.id LIKE 'cost_of_living.%%' OR s.id LIKE 'inflation.cpi.%%')
        WINDOW w AS (PARTITION BY o.series_id, o.dims ORDER BY o.obs_date)),
      g AS (SELECT *, sum(CASE WHEN same THEN 0 ELSE 1 END) OVER (PARTITION BY series_id, dims ORDER BY obs_date) grp FROM n),
      r AS (SELECT series_id, dims, grp, min(obs_date) d0, max(obs_date) d1, count(*) len, min(value) v, min(tier) tier
            FROM g GROUP BY 1, 2, 3)
      SELECT series_id, d0 AS obs_date, dims, v::float8 AS our_value, tier,
             jsonb_build_object('run_length', len, 'until', d1) AS context
      FROM r WHERE len >= 6""",
}


def ensure_tables() -> None:
    from app import db
    for ddl in DDL:
        db.execute(ddl)


def detect(commit: bool = False, only: list[str] | None = None) -> dict[str, int]:
    """Run the detectors. New findings are inserted; existing ones (and their verdicts) stay."""
    from app import db
    ensure_tables()
    counts: dict[str, int] = {}
    for name, sql in DETECTORS.items():
        if only and name not in only:
            continue
        t0 = time.time()
        rows = db.query(sql)
        counts[name] = len(rows)
        by_tier = Counter(r["tier"] for r in rows)
        print(f"{name:24s} {len(rows):7d}  tiers {dict(by_tier)}  ({time.time() - t0:.0f}s)")
        if commit and rows:
            with db.connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO dq_findings (series_id, obs_date, dims, detector, tier, our_value, context)
                       VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb)
                       ON CONFLICT (series_id, obs_date, dims, detector)
                       DO UPDATE SET our_value = EXCLUDED.our_value, context = EXCLUDED.context""",
                    [(r["series_id"], r["obs_date"], json.dumps(r["dims"] or {}), name, r["tier"],
                      r["our_value"], json.dumps(r["context"] or {}, default=str)) for r in rows],
                )
    if not commit:
        print("dry run — re-run with --commit to store findings")
    return counts


# ---- reconciliation ---------------------------------------------------------------------

@dataclass(frozen=True)
class Diff:
    obs_date: date
    kind: str  # value | missing_ours | missing_source
    ours: float | None
    source: float | None


def same_value(a: float | None, b: float | None, rel: float = 1e-6) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= max(abs(a), abs(b)) * rel + 1e-12


def compare_points(ours: dict[date, float | None], source: dict[date, float | None]) -> tuple[int, list[Diff]]:
    """Point-by-point comparison of one series. Returns (matched, diffs). Pure.

    A source date we lack is `missing_ours`; a date we hold that the source no longer
    lists is `missing_source` (a dropped point — or history from a merged older dataset)."""
    matched, diffs = 0, []
    for d in sorted(set(ours) | set(source)):
        if d not in source:
            diffs.append(Diff(d, "missing_source", ours[d], None))
        elif d not in ours:
            if source[d] is not None:
                diffs.append(Diff(d, "missing_ours", None, source[d]))
        elif same_value(ours[d], source[d]):
            matched += 1
        else:
            diffs.append(Diff(d, "value", ours[d], source[d]))
    return matched, diffs


def verdict_for(our_value: float | None, src: dict[date, float | None], d: date,
                scale_fixed_from: float | None = None) -> tuple[str, float | None]:
    """Verdict for one flagged point once the source series is in hand. Pure."""
    if d not in src:
        return "not_in_source", None
    sv = src[d]
    if same_value(our_value, sv):
        return "confirmed_as_published", sv
    if scale_fixed_from is not None and same_value(scale_fixed_from, sv):
        return "scale_corrected", sv
    return "source_differs", sv


# EasyData series our flagship copies are made from (derive_kibor -> rates.kibor.*,
# derive_auctions -> rates.auction.*): reconciled first, so `resolve-copies` can settle the
# tier-1 findings on the copies.
COPY_INPUTS_LIKE = ["banking.%karachi_interbank_%", "banking.cut_off_yield%"]


def _candidates(limit: int, include_all: bool) -> list[dict]:
    """EasyData series to reconcile: inputs of tier-1 copies not yet checked, series whose
    names collide, then series with unverified findings (by finding count), then — with --all — every other active
    EasyData series, oldest check first."""
    from app import db
    inputs = db.query(
        """SELECT s.id FROM series s LEFT JOIN dq_series_checks c ON c.series_id = s.id AND c.method = 'easydata'
           WHERE s.is_active AND s.easydata_dataset_code IS NOT NULL AND c.series_id IS NULL
             AND s.id LIKE ANY(%(likes)s) ORDER BY s.id""", {"likes": COPY_INPUTS_LIKE})
    flagged = db.query(
        """SELECT f.series_id, min(f.tier) tier, count(*) n FROM dq_findings f
           JOIN series s ON s.id = f.series_id
           WHERE f.verdict IS NULL AND s.easydata_dataset_code IS NOT NULL AND s.is_active
           GROUP BY 1 ORDER BY 2, 3 DESC""")
    # series whose names collide: their SBP series/table names settle the rename
    collide = db.query(
        """SELECT s.id FROM series s LEFT JOIN dq_series_checks c ON c.series_id = s.id AND c.method = 'easydata'
           WHERE s.is_active AND s.easydata_dataset_code IS NOT NULL AND c.series_id IS NULL
             AND s.name IN (SELECT name FROM series WHERE is_active GROUP BY 1 HAVING count(*) > 1) ORDER BY s.name""")
    out = [r["id"] for r in inputs]
    out += [r["id"] for r in collide if r["id"] not in set(out)]
    out += [r["series_id"] for r in flagged if r["series_id"] not in set(out)]
    if include_all:
        seen = set(out)
        rest = db.query(
            """SELECT s.id FROM series s LEFT JOIN dq_series_checks c
                 ON c.series_id = s.id AND c.method = 'easydata'
               WHERE s.is_active AND s.easydata_dataset_code IS NOT NULL
               ORDER BY c.checked_at NULLS FIRST,
                        array_position(ARRAY['forex','monetary','prices','fixed-income','debt','social',
                                             'public-finance','real','alternate','external'], s.module)""")
        out += [r["id"] for r in rest if r["id"] not in seen]
    return [{"series_id": s} for s in out[:limit]]


def _fetch_easydata(job, quota, key: str) -> dict[date, float | None]:
    from ingestion.jobs.easydata_sync import _parse_date, _parse_value, easydata_rows

    def url(api_key: str) -> str:
        return (f"https://easydata.sbp.org.pk/api/v1/series/{quote(key, safe='')}/data?api_key={api_key}"
                f"&start_date=1940-01-01&end_date={date.today().isoformat()}&format=json")

    text = job._http_get_rotating(quota, url).decode("utf-8", "replace")
    m = _FIRST_ROW.search(text)
    if m:
        _DATASET_NAMES[key.split(".", 1)[0]] = m.group(1).strip()
        _SERIES_NAMES[key] = m.group(3).strip()
    return {_parse_date(d): _parse_value(v) for k, d, v in easydata_rows(text) if k == key}


# The first row's "Dataset Name" and "Series Name" — captured while reconciling so series
# whose composed names collide can be told apart by their SBP table ("Monetary Survey" vs
# "Reserve Money") or by SBP's own series name (scripts/disambiguate_series_names.py).
_FIRST_ROW = __import__("re").compile(r'"rows"\s*:\s*\[\s*\[\s*"([^"]+)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"')
_DATASET_NAMES: dict[str, str] = {}
_SERIES_NAMES: dict[str, str] = {}


def _save_dataset_names() -> None:
    from app import db
    if _DATASET_NAMES:
        db.execute("CREATE TABLE IF NOT EXISTS easydata_datasets (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
        with db.connection() as conn, conn.cursor() as cur:
            cur.executemany("INSERT INTO easydata_datasets (code, name) VALUES (%s, %s) "
                            "ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name", list(_DATASET_NAMES.items()))
    if _SERIES_NAMES:
        db.execute("CREATE TABLE IF NOT EXISTS easydata_series_names (easydata_key TEXT PRIMARY KEY, name TEXT NOT NULL)")
        with db.connection() as conn, conn.cursor() as cur:
            cur.executemany("INSERT INTO easydata_series_names (easydata_key, name) VALUES (%s, %s) "
                            "ON CONFLICT (easydata_key) DO UPDATE SET name = EXCLUDED.name", list(_SERIES_NAMES.items()))


def probe_easydata(series_ids: list[str]) -> None:
    """Print the shape of EasyData's raw response for series the sync fails to parse:
    columns, row lengths, and the text around a JSON error. No DB writes."""
    from ingestion.easydata_config import load_series
    from ingestion.jobs.easydata_sync import EasyDataSyncJob
    from ingestion.quota import QuotaCounter, QuotaPool
    job = EasyDataSyncJob(quota_path="./data/dq_quota.json")
    quota = QuotaPool(job.api_keys, base_path="./data/dq_quota.json") if len(job.api_keys) > 1 \
        else QuotaCounter("./data/dq_quota.json")
    key_of = {s.id: s.easydata_key for s in load_series()}
    for sid in series_ids:
        key = key_of.get(sid)
        print(f"== {sid} key={key}")
        if not key:
            continue
        raw = job._http_get_rotating(quota, lambda k: (
            f"https://easydata.sbp.org.pk/api/v1/series/{quote(key, safe='')}/data?api_key={k}"
            f"&start_date=1940-01-01&end_date={date.today().isoformat()}&format=json")).decode("utf-8", "replace")
        print(f"bytes={len(raw)} head={raw[:300]!r}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"JSON error: {exc}; around: {raw[max(0, exc.pos - 200):exc.pos + 120]!r}")
            continue
        cols = payload.get("columns", [])
        rows = payload.get("rows", [])
        print(f"columns={cols}")
        print(f"rows={len(rows)} row lengths={dict(Counter(len(r) for r in rows))}")
        short = [r for r in rows if len(r) < len(cols)]
        print(f"short rows (first 5): {short[:5]}")
        print(f"last rows: {rows[-3:]}")


def _write_source_values(job, sid: str, src: dict[date, float | None], stats: Counter) -> None:
    """Write SBP's current figure for the points of `sid` that differ or that we lack, through
    the sync job's own validate + upsert (so they land as ordinary publisher revisions and
    additions). Our deliberate unit-slip fixes and points SBP no longer lists are left alone.
    The job's per-call alert emails are collected into one summary instead."""
    from app import db
    from ingestion import framework
    from ingestion.framework import Record
    fixed = {r["obs_date"] for r in db.query("SELECT obs_date FROM obs_scale_corrections WHERE series_id=%s", (sid,))}
    ours = {r["obs_date"]: r["v"] for r in db.query(
        "SELECT obs_date, value::float8 v FROM observations WHERE series_id=%s AND dims='{}'::jsonb", (sid,))}
    _, diffs = compare_points(ours, src)
    recs = [Record(sid, d.obs_date, d.source, {}) for d in diffs
            if d.kind in ("value", "missing_ours") and d.obs_date not in fixed]
    if not recs:
        return
    real_alert = framework.alerting.alert
    framework.alerting.alert = lambda subject, body="": _ALERTS.append(f"{subject}: {body}"[:300])
    try:
        stats["points_written"] += job.upsert(job.validate(recs))
    finally:
        framework.alerting.alert = real_alert


_ALERTS: list[str] = []


def reconcile_easydata(budget: int = 500, include_all: bool = False, commit: bool = False,
                       deadline_s: int = 50 * 60, apply: bool = False) -> Counter:
    """Pull each candidate series' full history from EasyData and compare every point; with
    `apply`, write SBP's current figure where ours differs or is missing."""
    from app import db
    from ingestion.easydata_config import load_series
    from ingestion.jobs.easydata_sync import EasyDataSyncJob, QuotaExhausted
    from ingestion.quota import QuotaCounter, QuotaPool

    ensure_tables()
    job = EasyDataSyncJob(quota_path="./data/dq_quota.json")
    if not job.api_keys:
        raise SystemExit("EASYDATA_API_KEY(S) not set")
    quota = QuotaPool(job.api_keys, base_path="./data/dq_quota.json") if len(job.api_keys) > 1 \
        else QuotaCounter("./data/dq_quota.json")
    key_of = {s.id: s.easydata_key for s in load_series()}
    stats: Counter = Counter()
    t_end = time.time() + deadline_s
    for c in _candidates(budget, include_all):
        sid = c["series_id"]
        key = key_of.get(sid)
        if key is None:
            stats["no_easydata_key"] += 1
            continue
        if time.time() > t_end or not quota.allow():
            stats["stopped_budget"] += 1
            break
        try:
            src = _fetch_easydata(job, quota, key)
        except QuotaExhausted:
            stats["stopped_quota"] += 1
            break
        except Exception as exc:  # noqa: BLE001 — one bad series never stops the audit
            stats["fetch_error"] += 1
            if commit:
                db.execute(
                    """INSERT INTO dq_series_checks (series_id, method, checked_at, error) VALUES (%s,'easydata',now(),%s)
                       ON CONFLICT (series_id, method) DO UPDATE SET checked_at=now(), error=EXCLUDED.error""",
                    (sid, job.redact(str(exc))[:300]))
            continue
        if commit and apply:
            _write_source_values(job, sid, src, stats)
        _store_reconciliation(sid, "easydata", src, commit, stats, evidence=f"easydata:{key}")
    if commit:
        _save_dataset_names()
    if _ALERTS:
        from ingestion import alerting
        alerting.alert(f"dq_audit: {len(_ALERTS)} validation notices while writing SBP values",
                       "\n".join(_ALERTS[:20]))
    print(dict(stats))
    if not commit:
        print("dry run — re-run with --commit to store results")
    return stats


def apply_easydata(budget: int = 500, commit: bool = False, deadline_s: int = 50 * 60) -> Counter:
    """Bring series with reconciliation differences in line with SBP: re-fetch each one,
    write the points that differ or that we lack (through the sync job's validate +
    upsert, so they are ordinary publisher revisions/additions), then re-check it.
    Points we hold that SBP no longer lists, and our deliberate unit-slip fixes, are
    left alone."""
    from app import db
    from ingestion.easydata_config import load_series
    from ingestion.jobs.easydata_sync import EasyDataSyncJob, QuotaExhausted
    from ingestion.quota import QuotaCounter, QuotaPool

    job = EasyDataSyncJob(quota_path="./data/dq_quota.json")
    quota = QuotaPool(job.api_keys, base_path="./data/dq_quota.json") if len(job.api_keys) > 1 \
        else QuotaCounter("./data/dq_quota.json")
    key_of = {s.id: s.easydata_key for s in load_series()}
    todo = [r["series_id"] for r in db.query(
        """SELECT series_id, count(*) n FROM dq_mismatches WHERE method='easydata' AND kind IN ('value','missing_ours')
           GROUP BY 1 ORDER BY 2 DESC LIMIT %s""", (budget,))]
    stats: Counter = Counter()
    t_end = time.time() + deadline_s
    for sid in todo:
        if time.time() > t_end or not quota.allow():
            stats["stopped_budget"] += 1
            break
        key = key_of.get(sid)
        if not key:
            stats["no_easydata_key"] += 1
            continue
        try:
            src = _fetch_easydata(job, quota, key)
        except QuotaExhausted:
            stats["stopped_quota"] += 1
            break
        except Exception:  # noqa: BLE001
            stats["fetch_error"] += 1
            continue
        stats["series_applied"] += 1
        if commit:
            _write_source_values(job, sid, src, stats)
            _store_reconciliation(sid, "easydata", src, True, stats, evidence=f"easydata:{key}")
    if _ALERTS:
        from ingestion import alerting
        alerting.alert(f"dq_audit: {len(_ALERTS)} validation notices while writing SBP values",
                       "\n".join(_ALERTS[:20]))
    print(dict(stats))
    if not commit:
        print("dry run — re-run with --commit to write")
    return stats


def _store_reconciliation(sid: str, method: str, src: dict[date, float | None], commit: bool,
                          stats: Counter, evidence: str, dims: str = "{}", partial: bool = False) -> None:
    """Compare one (series, dims) with the source's points and store the outcome. `partial`:
    the source covers only some dates (a set of files), so only those dates are compared."""
    from app import db
    rows = db.query("SELECT obs_date, value::float8 v FROM observations WHERE series_id=%s AND dims=%s::jsonb",
                    (sid, dims))
    ours = {r["obs_date"]: r["v"] for r in rows if not partial or r["obs_date"] in src}
    matched, diffs = compare_points(ours, src)
    kinds = Counter(d.kind for d in diffs)
    stats["series"] += 1
    stats["points_matched"] += matched
    for k, n in kinds.items():
        stats[f"points_{k}"] += n
    if kinds.get("value"):
        stats["series_with_value_diffs"] += 1
    fixed = {r["obs_date"]: float(r["old_value"]) for r in db.query(
        "SELECT obs_date, old_value FROM obs_scale_corrections WHERE series_id=%s AND dims=%s::jsonb", (sid, dims))}
    findings = db.query("SELECT id, obs_date, our_value::float8 v FROM dq_findings WHERE series_id=%s "
                        "AND dims=%s::jsonb AND verdict IS NULL", (sid, dims))
    verdicts = [(fid["id"], *verdict_for(fid["v"], src, fid["obs_date"], fixed.get(fid["obs_date"])))
                for fid in findings if not partial or fid["obs_date"] in src]
    for _, v, _ in verdicts:
        stats[f"verdict_{v}"] += 1
    if not commit:
        return
    with db.connection() as conn, conn.cursor() as cur:
        if partial:
            cur.execute("DELETE FROM dq_mismatches WHERE series_id=%s AND method=%s AND dims=%s::jsonb "
                        "AND obs_date = ANY(%s)", (sid, method, dims, list(src)))
        else:
            cur.execute("DELETE FROM dq_mismatches WHERE series_id=%s AND method=%s AND dims=%s::jsonb",
                        (sid, method, dims))
        if diffs:
            cur.executemany(
                """INSERT INTO dq_mismatches (series_id, obs_date, dims, method, kind, our_value, source_value)
                   VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)""",
                [(sid, d.obs_date, dims, method,
                  "scale_corrected" if d.kind == "value" and same_value(fixed.get(d.obs_date), d.source) else d.kind,
                  d.ours, d.source) for d in diffs])
        if partial:  # dq_series_checks is per series: accumulate the files' coverage instead
            if verdicts:
                cur.executemany(
                    "UPDATE dq_findings SET verdict=%s, source_value=%s, evidence=%s, checked_at=now() WHERE id=%s",
                    [(v, sv, evidence, fid) for fid, v, sv in verdicts])
            return
        cur.execute(
            """INSERT INTO dq_series_checks (series_id, method, checked_at, source_points, our_points, matched,
                                            mismatched, missing_ours, missing_source, error)
               VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, NULL)
               ON CONFLICT (series_id, method) DO UPDATE SET checked_at=now(), source_points=EXCLUDED.source_points,
                 our_points=EXCLUDED.our_points, matched=EXCLUDED.matched, mismatched=EXCLUDED.mismatched,
                 missing_ours=EXCLUDED.missing_ours, missing_source=EXCLUDED.missing_source, error=NULL""",
            (sid, method, len(src), len(ours), matched, kinds.get("value", 0), kinds.get("missing_ours", 0),
             kinds.get("missing_source", 0)))
        if verdicts:
            cur.executemany(
                "UPDATE dq_findings SET verdict=%s, source_value=%s, evidence=%s, checked_at=now() WHERE id=%s",
                [(v, sv, evidence, fid) for fid, v, sv in verdicts])


def compare_records(method: str, path: str, commit: bool = False) -> Counter:
    """Compare records parsed from the publisher's own files (by the job's parser, on a
    machine that can reach the source) with what we store — only the dates the files cover.
    JSON list of {series_id, obs_date, value, dims}. Records go through the job's id
    canonicalisation first."""
    from app import db
    from ingestion.framework import canonicalize_id
    ensure_tables()
    groups: dict[tuple[str, str], dict[date, float | None]] = defaultdict(dict)
    for r in json.loads(open(path, encoding="utf-8").read()):
        dims = json.dumps(r.get("dims") or {}, sort_keys=True)
        groups[(canonicalize_id(r["series_id"]), dims)][date.fromisoformat(r["obs_date"])] = r["value"]
    known = {r["id"] for r in db.query("SELECT id FROM series WHERE id = ANY(%s)", (list({s for s, _ in groups}),))}
    stats: Counter = Counter()
    per_series: dict[str, Counter] = defaultdict(Counter)
    for (sid, dims), src in sorted(groups.items()):
        if sid not in known:
            stats["not_in_catalog"] += 1
            continue
        # the DB stores dims as jsonb; compare on its canonical text form
        dims_db = db.query_one("SELECT %s::jsonb::text AS d", (dims,))["d"]
        c: Counter = Counter()
        _store_reconciliation(sid, method, src, commit, c, evidence=f"{method}:{path.rsplit('/', 1)[-1]}",
                              dims=dims_db, partial=True)
        per_series[sid].update(c)
        stats.update(c)
    if commit:
        with db.connection() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO dq_series_checks (series_id, method, checked_at, source_points, our_points, matched,
                                                mismatched, missing_ours, missing_source, error)
                   VALUES (%s, %s, now(), %s, %s, %s, %s, %s, 0, NULL)
                   ON CONFLICT (series_id, method) DO UPDATE SET checked_at=now(), source_points=EXCLUDED.source_points,
                     our_points=EXCLUDED.our_points, matched=EXCLUDED.matched, mismatched=EXCLUDED.mismatched,
                     missing_ours=EXCLUDED.missing_ours, missing_source=0, error=NULL""",
                [(sid, method, c["points_matched"] + c["points_value"] + c["points_missing_ours"],
                  c["points_matched"] + c["points_value"], c["points_matched"], c["points_value"],
                  c["points_missing_ours"]) for sid, c in per_series.items()])
    stats.pop("series", None)
    print({"series": len(per_series), **dict(stats)})
    if not commit:
        print("dry run — re-run with --commit to store results")
    return stats


def compare_fund_navs(path: str, commit: bool = False) -> Counter:
    """Every fund's NAV history as MUFAP serves it today ({fund_id: [[date, nav], ...]},
    fetched raw on a workstation) vs `fund_navs`. A MUFAP point we hold in
    `fund_navs_quarantine` instead is reported as `quarantined` (the nightly spike rule
    removed it) so false quarantines show up."""
    from app import db
    ensure_tables()
    src_all = json.loads(open(path, encoding="utf-8").read())
    quarantined = defaultdict(dict)
    for r in db.query("SELECT fund_id, obs_date, nav::float8 v FROM fund_navs_quarantine"):
        quarantined[r["fund_id"]][r["obs_date"]] = r["v"]
    stats: Counter = Counter()
    for fid_s, pts in src_all.items():
        fid = int(fid_s)
        src = {date.fromisoformat(d): v for d, v in pts}
        ours = {r["obs_date"]: r["v"] for r in db.query(
            "SELECT obs_date, nav::float8 v FROM fund_navs WHERE fund_id=%s", (fid,))}
        matched, diffs = compare_points(ours, src)
        rows = []
        for d in diffs:
            kind = d.kind
            if kind == "missing_ours" and d.obs_date in quarantined.get(fid, {}):
                kind = "quarantined"
            rows.append((f"fund:{fid}", d.obs_date, kind, d.ours, d.source))
            stats[f"points_{kind}"] += 1
        stats["funds"] += 1
        stats["points_matched"] += matched
        if commit:
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM dq_mismatches WHERE series_id=%s AND method='mufap_nav'", (f"fund:{fid}",))
                if rows:
                    cur.executemany(
                        """INSERT INTO dq_mismatches (series_id, obs_date, dims, method, kind, our_value, source_value)
                           VALUES (%s, %s, '{}'::jsonb, 'mufap_nav', %s, %s, %s)""", rows)
                cur.execute(
                    """INSERT INTO dq_series_checks (series_id, method, checked_at, source_points, our_points, matched,
                                                    mismatched, missing_ours, missing_source, error)
                       VALUES (%s, 'mufap_nav', now(), %s, %s, %s, %s, %s, %s, NULL)
                       ON CONFLICT (series_id, method) DO UPDATE SET checked_at=now(), source_points=EXCLUDED.source_points,
                         our_points=EXCLUDED.our_points, matched=EXCLUDED.matched, mismatched=EXCLUDED.mismatched,
                         missing_ours=EXCLUDED.missing_ours, missing_source=EXCLUDED.missing_source, error=NULL""",
                    (f"fund:{fid}", len(src), len(ours), matched, sum(1 for r in rows if r[2] == "value"),
                     sum(1 for r in rows if r[2] in ("missing_ours", "quarantined")),
                     sum(1 for r in rows if r[2] == "missing_source")))
    print(dict(stats))
    return stats


def apply_fund_navs(path: str, commit: bool = False) -> Counter:
    """Bring fund NAV histories in line with MUFAP — only for funds whose MUFAP history is
    trustworthy: its latest point equals the NAV we stored from MUFAP's official daily
    table on that date. (For a few fund ids MUFAP's history endpoint serves another fund's
    series — e.g. Atlas Islamic Income at ~102 while the daily table says 532 — those are
    reported and left alone.)

    For a trusted fund: values that differ are replaced, MUFAP points we lack are added
    (including points our nightly spike rule had quarantined: the rule re-runs and keeps
    out the real glitches), and points MUFAP doesn't list — e.g. another fund's history
    stored under a new fund's id, dated before it existed — are removed. Offer/repurchase
    from the daily snapshots are kept. Back up fund_navs + fund_navs_quarantine first."""
    from app import db
    src_all = {int(k): {date.fromisoformat(d): float(v) for d, v in pts}
               for k, pts in json.loads(open(path, encoding="utf-8").read()).items() if pts}
    stats: Counter = Counter()
    untrusted = []
    upserts, deletes, unquarantine = [], [], []
    for fid, src in src_all.items():
        ours = {r["obs_date"]: r["v"] for r in db.query(
            "SELECT obs_date, nav::float8 v FROM fund_navs WHERE fund_id=%s", (fid,))}
        last = max(src)
        if not ours or last not in ours or not same_value(ours[last], src[last]):
            untrusted.append(fid)
            continue
        _, diffs = compare_points(ours, src)
        for d in diffs:
            if d.kind in ("value", "missing_ours"):
                upserts.append((fid, d.obs_date, d.source))
                stats[f"nav_{d.kind}"] += 1
                if d.kind == "missing_ours":
                    unquarantine.append((fid, d.obs_date))
            elif d.kind == "missing_source" and d.obs_date < last:  # history can lag the daily table
                deletes.append((fid, d.obs_date))
                stats["nav_removed_not_at_source"] += 1
        stats["trusted_funds"] += 1
    stats["untrusted_funds"] = len(untrusted)
    print(dict(stats))
    print("left alone (MUFAP history disagrees with its own daily NAV):", untrusted)
    if not commit:
        print("dry run — re-run with --commit to write")
        return stats
    with db.connection() as conn, conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO fund_navs (fund_id, obs_date, nav) VALUES (%s, %s, %s)
               ON CONFLICT (fund_id, obs_date) DO UPDATE SET nav = EXCLUDED.nav, revised_at = now()
               WHERE fund_navs.nav IS DISTINCT FROM EXCLUDED.nav""", upserts)
        cur.executemany("DELETE FROM fund_navs WHERE fund_id=%s AND obs_date=%s", deletes)
        cur.executemany("DELETE FROM fund_navs_quarantine WHERE fund_id=%s AND obs_date=%s", unquarantine)
    print(f"wrote {len(upserts)} NAVs, removed {len(deletes)}")
    return stats


def compare_agri(path: str, commit: bool = False) -> Counter:
    """AMIS FQP histories re-fetched for a sample of (commodity, market) pairs — JSON list
    of {commodity_id, market_id, obs_date, fqp} — vs `agri_prices`. Points we hold in
    `agri_prices_quarantine` instead are `quarantined` (removed as typos at source)."""
    from app import db
    ensure_tables()
    by_pair: dict[tuple[int, int], dict[date, float]] = defaultdict(dict)
    for r in json.loads(open(path, encoding="utf-8").read()):
        by_pair[(r["commodity_id"], r["market_id"])][date.fromisoformat(r["obs_date"])] = r["fqp"]
    stats: Counter = Counter()
    for (cid, mid), src in by_pair.items():
        ours = {r["obs_date"]: r["v"] for r in db.query(
            "SELECT obs_date, fqp::float8 v FROM agri_prices WHERE commodity_id=%s AND market_id=%s "
            "AND fqp IS NOT NULL", (cid, mid))}
        quar = {r["obs_date"] for r in db.query(
            "SELECT obs_date FROM agri_prices_quarantine WHERE commodity_id=%s AND market_id=%s", (cid, mid))}
        # the history export ends a few days back; only compare dates both could hold
        last = max(src) if src else None
        ours = {d: v for d, v in ours.items() if last is None or d <= last}
        matched, diffs = compare_points(ours, src)
        rows = []
        for d in diffs:
            kind = "quarantined" if d.kind == "missing_ours" and d.obs_date in quar else d.kind
            stats[f"points_{kind}"] += 1
            rows.append((f"agri:{cid}:{mid}", d.obs_date, kind, d.ours, d.source))
        stats["pairs"] += 1
        stats["points_matched"] += matched
        if commit:
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute("DELETE FROM dq_mismatches WHERE series_id=%s AND method='amis_fqp'", (f"agri:{cid}:{mid}",))
                if rows:
                    cur.executemany(
                        """INSERT INTO dq_mismatches (series_id, obs_date, dims, method, kind, our_value, source_value)
                           VALUES (%s, %s, '{}'::jsonb, 'amis_fqp', %s, %s, %s)""", rows)
                cur.execute(
                    """INSERT INTO dq_series_checks (series_id, method, checked_at, source_points, our_points, matched,
                                                    mismatched, missing_ours, missing_source, error)
                       VALUES (%s, 'amis_fqp', now(), %s, %s, %s, %s, %s, %s, NULL)
                       ON CONFLICT (series_id, method) DO UPDATE SET checked_at=now(), source_points=EXCLUDED.source_points,
                         our_points=EXCLUDED.our_points, matched=EXCLUDED.matched, mismatched=EXCLUDED.mismatched,
                         missing_ours=EXCLUDED.missing_ours, missing_source=EXCLUDED.missing_source, error=NULL""",
                    (f"agri:{cid}:{mid}", len(src), len(ours), matched, sum(1 for r in rows if r[2] == "value"),
                     sum(1 for r in rows if r[2] in ("missing_ours", "quarantined")),
                     sum(1 for r in rows if r[2] == "missing_source")))
    print(dict(stats))
    return stats


# ---- copies + manual verdicts -----------------------------------------------------------

def _copy_sources() -> dict[tuple[str, str], str]:
    """{(flagship series id, dims json): EasyData input series id} for the derived copies."""
    from app import db
    from ingestion.jobs.derive_auctions import map_sources
    from ingestion.jobs.derive_kibor import map_source, target_id
    out: dict[tuple[str, str], str] = {}
    for r in db.query("SELECT id FROM series WHERE id LIKE 'banking.%%karachi_interbank_%%'"):
        m = map_source(r["id"])
        if m:
            out[(target_id(m[0]), json.dumps({"side": m[1]}))] = r["id"]
    rows = db.query("SELECT id, name, easydata_dataset_code FROM series "
                    "WHERE easydata_dataset_code IN ('TS_GP_BAM_SIRTBIL_AH', 'TS_GP_BAM_SIRPIBS_AH')")
    for sid, (atype, tenor) in map_sources(rows).items():
        out.setdefault((f"rates.auction.{atype}.{tenor}.cutoff_yield", "{}"), sid)
    return out


def resolve_copies(commit: bool = False) -> Counter:
    """Settle findings on rates.kibor.* / rates.auction.* (copies of EasyData series): the
    copy must equal its input, and the input must match SBP (reconciled, no mismatch on
    that date). Inputs not yet reconciled are left for a later pass."""
    from app import db
    srcs = _copy_sources()
    stats: Counter = Counter()
    updates = []
    for f in db.query("""SELECT id, series_id, obs_date, dims, our_value::float8 v FROM dq_findings
                         WHERE verdict IS NULL AND (series_id LIKE 'rates.kibor.%%' OR series_id LIKE 'rates.auction.%%')"""):
        src = srcs.get((f["series_id"], json.dumps(f["dims"] or {})))
        if not src:
            stats["no_input"] += 1  # e.g. a scraped (non-EasyData) point
            continue
        inp = db.query_one("SELECT value::float8 v FROM observations WHERE series_id=%s AND obs_date=%s "
                           "AND dims='{}'::jsonb", (src, f["obs_date"]))
        chk = db.query_one("SELECT 1 FROM dq_series_checks WHERE series_id=%s AND method='easydata' "
                           "AND error IS NULL", (src,))
        mism = db.query_one("SELECT kind, source_value::float8 sv FROM dq_mismatches WHERE series_id=%s "
                            "AND obs_date=%s AND method='easydata'", (src, f["obs_date"]))
        if inp is None or not same_value(inp["v"], f["v"]):
            updates.append(("copy_differs", inp["v"] if inp else None, f"copy of {src}", f["id"]))
        elif not chk:
            stats["input_not_reconciled"] += 1
            continue
        elif mism:
            updates.append(("source_differs", mism["sv"], f"easydata:{src}", f["id"]))
        else:
            updates.append(("confirmed_as_published", f["v"], f"easydata:{src}", f["id"]))
    for u in updates:
        stats[u[0]] += 1
    if commit and updates:
        with db.connection() as conn, conn.cursor() as cur:
            cur.executemany("UPDATE dq_findings SET verdict=%s, source_value=%s, evidence=%s, checked_at=now() "
                            "WHERE id=%s", updates)
    print(dict(stats))
    return stats


NOTES_DDL = """CREATE TABLE IF NOT EXISTS observation_notes (
    series_id TEXT NOT NULL, obs_date DATE NOT NULL, dims JSONB NOT NULL DEFAULT '{}', note TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'as_published', evidence TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, obs_date, dims))"""
_PUBLISHER = {"SBP": "SBP", "PBS": "PBS", "PTA": "PTA", "MUFAP": "MUFAP"}


def _times(x: float) -> str:
    """2 significant figures, thousands separated: 3643.8 -> '3,600'. Pure."""
    from math import floor, log10
    r = round(x, -int(floor(log10(abs(x)))) + 1) if x else 0
    return f"{int(r):,}" if r >= 100 else f"{r:g}"


def _exact_slip(value: float, prev: float, nxt: float | None) -> bool:
    """Dividing (or multiplying) by an exact power of 1,000 puts the value between its
    neighbours — the signature of a unit slip, not of a lumpy flow. Pure."""
    if nxt is None or not value:
        return False
    lo, hi = sorted((abs(prev), abs(nxt)))
    for k in (1, 2, 3):
        for v in (abs(value) / 1000 ** k, abs(value) * 1000 ** k):
            if lo * 0.8 <= v <= hi * 1.25:
                return True
    return False


def note_for(detector: str, value: float, context: dict, publisher: str, evidence: str | None) -> str | None:
    """Public note text for a finding confirmed as published, or None when it doesn't merit
    one (ordinary lumpy moves). Pure."""
    where = f"As published by {publisher}"
    if evidence and evidence.startswith("easydata:"):
        where += f" (EasyData {evidence.split(':', 1)[1]})"
    if detector == "spike_level":
        prev, nxt = context.get("prev"), context.get("next")
        if not prev or not value:
            return None
        ratio = max(abs(value / prev), abs(prev / value))
        if ratio < 100:
            return None
        direction = "above" if abs(value) > abs(prev) else "below"
        text = f"{where}, re-checked at the source. About {_times(ratio)} times {direction} the periods either side ({prev:g} and {nxt:g})"
        return text + (", which suggests a unit slip in the source's figure for this period." if _exact_slip(value, prev, nxt)
                       else ".")
    if detector == "out_of_range":
        return f"{where}, re-checked at the source. Unusual for a percentage measure."
    return None


def make_notes(manual_path: str | None = None, commit: bool = False) -> int:
    """Turn findings confirmed as published into public notes (meta.notes), plus hand-written
    notes from a JSON list of {series_id, obs_date, [dims], note, evidence}."""
    from app import db
    rows = db.query("""SELECT f.series_id, f.obs_date, f.dims, f.detector, f.our_value::float8 v, f.context, f.evidence,
                              s.source FROM dq_findings f JOIN series s ON s.id = f.series_id
                       WHERE f.verdict = 'confirmed_as_published' AND f.detector IN ('spike_level', 'out_of_range')""")
    notes = {}
    for r in rows:
        text = note_for(r["detector"], r["v"], r["context"] or {}, _PUBLISHER.get(r["source"], r["source"]), r["evidence"])
        if text:
            notes[(r["series_id"], r["obs_date"], json.dumps(r["dims"] or {}))] = (text, r["evidence"])
    # A series with several such points is a lumpy flow (dividends paid twice a year,
    # sporadic country-level transfers): there the extremes are the data, not anomalies.
    per_series = Counter(k[0] for k in notes)
    lumpy = {sid for sid, n in per_series.items() if n >= 3}
    notes = {k: v for k, v in notes.items() if k[0] not in lumpy}
    print(f"skipped {len(lumpy)} lumpy series")
    if manual_path:
        for it in json.loads(open(manual_path, encoding="utf-8").read()):
            notes[(it["series_id"], date.fromisoformat(it["obs_date"]), json.dumps(it.get("dims") or {}))] = (
                it["note"], it.get("evidence"))
    print(f"{len(notes)} notes on {len({k[0] for k in notes})} series")
    for (sid, d, _), (text, _) in list(notes.items())[:5]:
        print(f"  {sid} {d}: {text}")
    if not commit:
        print("dry run — re-run with --commit to write")
        return len(notes)
    db.execute(NOTES_DDL)
    with db.connection() as conn, conn.cursor() as cur:
        # notes are regenerated in full from the current findings (manual ones are re-supplied)
        cur.execute("DELETE FROM observation_notes WHERE kind = 'as_published'")
        cur.executemany(
            """INSERT INTO observation_notes (series_id, obs_date, dims, note, evidence) VALUES (%s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (series_id, obs_date, dims) DO UPDATE SET note = EXCLUDED.note, evidence = EXCLUDED.evidence""",
            [(sid, d, dims, text, ev) for (sid, d, dims), (text, ev) in notes.items()])
    return len(notes)


def mark(path: str, commit: bool = False) -> int:
    """Apply verdicts established by reading the source document directly. JSON list of
    {series_id, obs_date, [dims], verdict, [source_value], evidence}; applies to every
    detector's finding on that point."""
    from app import db
    items = json.loads(open(path, encoding="utf-8").read())
    n = 0
    with db.connection() as conn, conn.cursor() as cur:
        for it in items:
            cur.execute(
                """UPDATE dq_findings SET verdict=%s, source_value=%s, evidence=%s, checked_at=now()
                   WHERE series_id=%s AND obs_date=%s AND dims=%s::jsonb""",
                (it["verdict"], it.get("source_value"), it["evidence"], it["series_id"], it["obs_date"],
                 json.dumps(it.get("dims") or {})))
            n += cur.rowcount or 0
        if not commit:
            conn.rollback()
    print(f"{n} findings {'updated' if commit else 'would be updated (dry run)'} from {len(items)} verdicts")
    return n


# ---- report -----------------------------------------------------------------------------

def report(out_csv: str | None = None) -> None:
    from app import db
    ensure_tables()
    print("== findings by detector / verdict")
    for r in db.query("""SELECT detector, coalesce(verdict, '(unchecked)') v, count(*) n, count(DISTINCT series_id) s
                         FROM dq_findings GROUP BY 1, 2 ORDER BY 1, 3 DESC"""):
        print(f"  {r['detector']:24s} {r['v']:24s} {r['n']:7d} pts {r['s']:6d} series")
    print("== reconciliation (series checked / with value diffs / points)")
    for r in db.query("""SELECT method, count(*) series, count(*) FILTER (WHERE mismatched > 0) diff_series,
                                sum(matched) matched, sum(mismatched) mism, sum(missing_ours) mo, sum(missing_source) ms,
                                count(*) FILTER (WHERE error IS NOT NULL) errors
                         FROM dq_series_checks GROUP BY 1"""):
        print(f"  {r['method']}: {r['series']} series, {r['diff_series']} with value diffs; points matched "
              f"{r['matched']}, differ {r['mism']}, missing ours {r['mo']}, missing at source {r['ms']}; "
              f"errors {r['errors']}")
    n = db.query_one("""SELECT count(*) names, coalesce(sum(n), 0) series FROM (
                          SELECT name, count(*) n FROM series WHERE is_active GROUP BY 1 HAVING count(*) > 1) z""")
    print(f"== name collisions: {n['names']} names shared by {n['series']} active series")
    if out_csv:
        rows = db.query("""SELECT f.series_id, s.module, s.name, s.unit, f.obs_date, f.dims, f.detector, f.tier,
                                  f.our_value, f.context, f.verdict, f.source_value, f.evidence
                           FROM dq_findings f JOIN series s ON s.id = f.series_id
                           ORDER BY f.tier, s.module, f.series_id, f.obs_date""")
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(rows[0].keys() if rows else [])
            for r in rows:
                w.writerow([json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for v in r.values()])
        print(f"wrote {len(rows)} rows to {out_csv}")


def main(argv: list[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = [a for a in argv if not a.startswith("--")]
    flags = {a.split("=")[0]: (a.split("=", 1)[1] if "=" in a else True) for a in argv if a.startswith("--")}
    cmd = args[0] if args else ""
    commit = bool(flags.get("--commit"))
    if cmd == "detect":
        only = str(flags["--only"]).split(",") if "--only" in flags else None
        detect(commit, only)
    elif cmd == "reconcile-easydata":
        reconcile_easydata(int(flags.get("--budget", 500)), bool(flags.get("--all")), commit,
                           apply=bool(flags.get("--apply")))
    elif cmd == "apply-easydata":
        apply_easydata(int(flags.get("--budget", 500)), commit)
    elif cmd == "probe-easydata":
        probe_easydata(args[1:])
    elif cmd == "compare-records":
        compare_records(args[1], args[2], commit)
    elif cmd == "apply-fund-navs":
        apply_fund_navs(args[1], commit)
    elif cmd == "compare-agri":
        compare_agri(args[1], commit)
    elif cmd == "compare-fund-navs":
        compare_fund_navs(args[1], commit)
    elif cmd == "resolve-copies":
        resolve_copies(commit)
    elif cmd == "make-notes":
        make_notes(args[1] if len(args) > 1 else None, commit)
    elif cmd == "mark":
        mark(args[1], commit)
    elif cmd == "report":
        report(args[1] if len(args) > 1 else None)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
