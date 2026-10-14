"""dq_audit — find suspect values and confirm them against the publisher (2026-10-14).

`data_quality` (nightly) only auto-fixes a few unambiguous glitches. This script is the
audit behind it: it flags everything that *looks* wrong, then settles each case by
re-reading the official source, so a value is either confirmed as published, corrected,
or reported as unverifiable — never left at "suspicious".

  python -m scripts.dq_audit detect [--commit]            # VPS, api container: run the detectors
  python -m scripts.dq_audit reconcile-easydata [--budget N] [--all] [--commit]
                                                          # GHA (SBP blocks the VPS): every stored
                                                          # point of each SBP EasyData series vs
                                                          # EasyData's current figures
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
from collections import Counter
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


def _candidates(limit: int, include_all: bool) -> list[dict]:
    """EasyData series to reconcile: those with unverified findings first (tier 1 then by
    finding count), then — with --all — every other active EasyData series, oldest check
    first."""
    from app import db
    flagged = db.query(
        """SELECT f.series_id, min(f.tier) tier, count(*) n FROM dq_findings f
           JOIN series s ON s.id = f.series_id
           WHERE f.verdict IS NULL AND s.easydata_dataset_code IS NOT NULL AND s.is_active
           GROUP BY 1 ORDER BY 2, 3 DESC""")
    out = [r["series_id"] for r in flagged]
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
    from ingestion.jobs.easydata_sync import _col_index, _parse_date, _parse_value

    def url(api_key: str) -> str:
        return (f"https://easydata.sbp.org.pk/api/v1/series/{quote(key, safe='')}/data?api_key={api_key}"
                f"&start_date=1940-01-01&end_date={date.today().isoformat()}&format=json")

    payload = json.loads(job._http_get_rotating(quota, url))
    cols = payload["columns"]
    ki, di, vi = (_col_index(cols, c) for c in ("Series Key", "Observation Date", "Observation Value"))
    out: dict[date, float | None] = {}
    for row in payload.get("rows", []):
        if str(row[ki]).strip() == key:
            out[_parse_date(row[di])] = _parse_value(row[vi])
    return out


def reconcile_easydata(budget: int = 500, include_all: bool = False, commit: bool = False,
                       deadline_s: int = 50 * 60) -> Counter:
    """Pull each candidate series' full history from EasyData and compare every point."""
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
        _store_reconciliation(sid, "easydata", src, commit, stats, evidence=f"easydata:{key}")
    print(dict(stats))
    if not commit:
        print("dry run — re-run with --commit to store results")
    return stats


def _store_reconciliation(sid: str, method: str, src: dict[date, float | None], commit: bool,
                          stats: Counter, evidence: str) -> None:
    from app import db
    rows = db.query("SELECT obs_date, value::float8 v FROM observations WHERE series_id=%s AND dims='{}'::jsonb",
                    (sid,))
    ours = {r["obs_date"]: r["v"] for r in rows}
    matched, diffs = compare_points(ours, src)
    kinds = Counter(d.kind for d in diffs)
    stats["series"] += 1
    stats["points_matched"] += matched
    for k, n in kinds.items():
        stats[f"points_{k}"] += n
    if kinds.get("value"):
        stats["series_with_value_diffs"] += 1
    fixed = {r["obs_date"]: float(r["old_value"]) for r in db.query(
        "SELECT obs_date, old_value FROM obs_scale_corrections WHERE series_id=%s", (sid,))}
    findings = db.query("SELECT id, obs_date, our_value::float8 v FROM dq_findings WHERE series_id=%s "
                        "AND dims='{}'::jsonb AND verdict IS NULL", (sid,))
    verdicts = [(fid["id"], *verdict_for(fid["v"], src, fid["obs_date"], fixed.get(fid["obs_date"])))
                for fid in findings]
    for _, v, _ in verdicts:
        stats[f"verdict_{v}"] += 1
    if not commit:
        return
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM dq_mismatches WHERE series_id=%s AND method=%s", (sid, method))
        if diffs:
            cur.executemany(
                """INSERT INTO dq_mismatches (series_id, obs_date, dims, method, kind, our_value, source_value)
                   VALUES (%s, %s, '{}'::jsonb, %s, %s, %s, %s)""",
                [(sid, d.obs_date, method,
                  "scale_corrected" if d.kind == "value" and same_value(fixed.get(d.obs_date), d.source) else d.kind,
                  d.ours, d.source) for d in diffs])
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
        reconcile_easydata(int(flags.get("--budget", 500)), bool(flags.get("--all")), commit)
    elif cmd == "report":
        report(args[1] if len(args) > 1 else None)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
