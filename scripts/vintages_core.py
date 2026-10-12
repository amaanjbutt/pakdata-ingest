"""Source-agnostic core of the revision-vintage backfill.

Every vintage source (SBP Monthly Statistical Bulletin, its chapter tables, PBS bulletins,
...) has its own adapter that downloads issues and parses them into one JSON shape:

    [{"issue": "2025-03", "published": "2025-03-31", "source": "sbp_msb", "freq": "monthly",
      "rows": [{"label": "...", "values": {"2025-01-31": [24.1, 1], ...}}]}, ...]

(`values` maps a period end date to [printed value, decimals printed].) This module maps
those rows to our series BY VALUE (labels and bases change over 20 years; values don't),
builds each point's chain of printed figures across issues and sources, and writes
`observations.first_seen_at` + `observation_revisions` (tagged with the source).

    python -m scripts.vintages_core apply  A.json [B.json ...] [--commit]
    python -m scripts.vintages_core candidates A.json [B.json ...] OUT.csv

`apply --commit` is a FULL REBUILD of every backfill source it is given: it refuses to run
unless all sources already in observation_revisions are passed, so one source's re-run
can't wipe another's dates. Rollback: DELETE FROM observation_revisions WHERE source IS NOT
NULL; restore first_seen_at from vintage_first_seen_bak_sbp_msb (the backup every commit
extends; the name is historical — it covers every source).

`candidates` lists bulletin rows that match NO series — figures we don't serve yet, to be
reviewed and seeded as new series (they then get vintages from the same prints).
"""
from __future__ import annotations

import csv
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MON_IDX = {m.lower(): i + 1 for i, m in enumerate(MONTHS)}
DEFAULT_SOURCE = "sbp_msb"
BAK_TABLE = "vintage_first_seen_bak_sbp_msb"


def month_end(y: int, m: int) -> date:
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


# Bulletin units vs ours: Billion Rs vs pkr_mn (x1000), million USD vs usd_mn (x1), etc.
SCALES = (1.0, 1e3, 1e-3, 1e6, 1e-6)
_MIN_MATCH_SHARE = 0.6   # a row maps to a series only if >=60% of its months match
_MIN_MATCHES = 4


def _sig3(v: float) -> str:
    return "0" if v == 0 else f"{v:.2e}"


def _same(db_value: float, published: float, dp: int, scale: float) -> bool:
    """Does our (full-precision) value print as the bulletin's figure?"""
    return abs(round(db_value / scale, dp) - published) <= 0.6 * 10 ** (-dp)


def build_index(rows: list[dict]) -> dict[tuple[str, str], list[tuple[str, float]]]:
    idx: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for r in rows:
        idx[(r["obs_date"].isoformat(), _sig3(float(r["value"])))].append((r["series_id"], float(r["value"])))
    return idx


def map_row(row: dict, idx) -> list[tuple[str, float, int]]:
    """Series (and unit scale) whose current values print as this row, by vote."""
    votes: dict[tuple[str, float], int] = defaultdict(int)
    n = 0
    for d, (pub, dp) in row["values"].items():
        # Small whole numbers ("6", "-1") match far too many series by chance; they
        # neither vote nor count toward the row's match share.
        if dp == 0 and abs(pub) < 100:
            continue
        n += 1
        for scale in SCALES:
            raw = pub * scale
            seen = set()
            for probe in (raw, raw * 1.004, raw * 0.996):
                for sid, dbv in idx.get((d, _sig3(probe)), ()):
                    if (sid, scale) not in seen and _same(dbv, pub, dp, scale):
                        seen.add((sid, scale))
                        votes[(sid, scale)] += 1
    if not votes:
        return []
    need = max(_MIN_MATCHES, math.ceil(_MIN_MATCH_SHARE * n))
    # Every series that clears the bar, not just the top vote: several of our ids carry
    # the same official figure (e.g. remittances under two datasets), and taking only
    # the winner left each twin with gaps — whole years with first prints dated late.
    return [(sid, scale, v) for (sid, scale), v in votes.items() if v >= need]


_UNIT_WORDS = {"rs", "billion", "million", "us", "dollars", "dollar", "percent", "base", "unit"}


def label_family(label: str) -> frozenset[str]:
    """Word set of a row label, ignoring case, punctuation, footnote marks, hyphens
    and unit words: "Non-Food" == "Non Food", "end month" == "Month end"."""
    words = re.findall(r"[a-z]+", label.lower().replace("-", " "))
    # Base years ("2010 = 100", "2015-16=100") are part of the identity: an index
    # rebased to a new year is a different series, not a revision of the old one.
    years = re.findall(r"(?:19|20)\d{2}", label)
    return frozenset([w for w in words if len(w) > 1 and w not in _UNIT_WORDS] + years)


def drop_blips(prints: list[tuple], scale: float) -> list[tuple]:
    """Drop a figure printed in one issue only that differs from the issues either
    side of it while those two agree ("A B A"): a parse slip, not a revision. A
    misprint that persists across issues (SBP's 36.36% lending rate) is kept."""
    def same(a, b):
        dp = min(a[2], b[2])
        return round(a[1] / scale, dp) == round(b[1] / scale, dp)

    out = []
    for i, p in enumerate(prints):
        if 0 < i < len(prints) - 1 and same(prints[i - 1], prints[i + 1]) and not same(prints[i - 1], p):
            continue
        out.append(p)
    return out


_NEAR_REL = 0.15
_TRAILING_REL = 0.15            # last print vs today: bigger gaps aren't recorded
_MAX_BIG_TRAILING_SHARE = 0.05  # >5% of a series' points like that -> series excluded


def near_match(row: dict, sid: str, scale: float, current: dict[tuple[str, str], float]) -> bool:
    """>=60% of the row's (informative) months within 15% of our current value."""
    hits = n = 0
    for d, (pub, dp) in row["values"].items():
        if dp == 0 and abs(pub) < 100:
            continue
        cur = current.get((sid, d))
        if cur is None:
            continue
        n += 1
        if abs(pub * scale - cur) <= _NEAR_REL * max(abs(cur), 1e-9):
            hits += 1
    return n >= _MIN_MATCHES and hits >= math.ceil(_MIN_MATCH_SHARE * n)


_OUTLIER_REL = 0.5        # printed figure >50% away from today's value...
_OUTLIER_MIN_ISSUES = 3   # ...counts only if at least this many issues printed it


def drop_unconfirmed_outliers(prints: list[tuple], current: float, scale: float) -> list[tuple]:
    """A figure far from today's value is kept only when several issues printed it
    (a real misprint or a big revision persists; a misread cell doesn't). E.g. SBP's
    36.36% lending rate stood for 8 issues -> kept; a one-issue 11,156.8 remittance
    figure or a two-issue 79.0 savings stock -> dropped."""
    def key(p):
        return round(p[1] / scale, p[2])

    counts: dict[float, int] = defaultdict(int)
    for p in prints:
        counts[key(p)] += 1
    out = []
    for p in prints:
        far = abs(p[1] - current) > _OUTLIER_REL * max(abs(current), 1e-9)
        if far and counts[key(p)] < _OUTLIER_MIN_ISSUES:
            continue
        out.append(p)
    return out


def _source_prints(issues: list[dict], idx, current: dict[tuple[str, str], float],
                   index_sids: frozenset[str], explicit: dict | None = None):
    """One source's issues -> (mapping {sid: info}, prints {(sid, date): [(pub, value, dp)]},
    unmatched rows [(issue, row)]). Prints are de-blipped and outlier-filtered.

    `explicit` {(source, label family): (sid, scale)} maps rows straight to a series —
    used for series seeded FROM the bulletin (see `seed`), whose label changed over the
    years; those rows skip value matching."""
    explicit = explicit or {}
    forced: list[tuple[dict, dict, str, float]] = []
    rest: list[tuple[dict, dict]] = []
    for iss in issues:
        for row in iss["rows"]:
            hit = explicit.get((iss.get("source", DEFAULT_SOURCE), label_family(row["label"])))
            if hit:
                forced.append((iss, row, hit[0], hit[1]))
            else:
                rest.append((iss, row))
    mapping: dict[str, dict] = {}
    published: dict[tuple[str, str], list[tuple[str, float, int]]] = defaultdict(list)
    forced_sids = {f[2] for f in forced}
    # value matching never lands on a seeded series: its rows are mapped explicitly
    matches = [(iss, row, sid, scale)
               for iss, row in rest for sid, scale, _v in map_row(row, idx) if sid not in forced_sids]
    # A series takes rows from ONE label family only (its dominant one). Value matching
    # can occasionally pair the wrong row — e.g. one issue's REER row against our NEER —
    # and a different concept must never leak into a series' history.
    families: dict[str, defaultdict] = defaultdict(lambda: defaultdict(int))
    for _iss, row, sid, _scale in matches:
        families[sid][label_family(row["label"])] += 1
    dominant = {sid: max(c, key=c.get) for sid, c in families.items()}
    # ...and ONE unit scale (its most common): mixing units in one series would make
    # the printed-precision comparisons below meaningless.
    scales: dict[str, defaultdict] = defaultdict(lambda: defaultdict(int))
    for _iss, _row, sid, scale in matches:
        scales[sid][scale] += 1
    scale_of = {sid: max(c, key=c.get) for sid, c in scales.items()}
    matches = [m for m in matches if m[3] == scale_of[m[2]]]
    # Second, looser pass: heavily revised series (BOP exports/imports, FDI) rarely
    # match today's values exactly in old issues — the revisions are the point. Once a
    # series has a confirmed label family and unit scale, accept other issues' rows of
    # that SAME family if most months are within 15% of today's value (same magnitude
    # and definition; a unit or methodology break fails this).
    matched = {(id(iss), id(row), sid) for iss, row, sid, _s in matches}
    for iss, row in rest:
        fam = label_family(row["label"])
        for sid, dfam in dominant.items():
            if dfam != fam or (id(iss), id(row), sid) in matched:
                continue
            if sid in index_sids:
                continue  # rebased indices can sit within 15% of each other — exact only
            if near_match(row, sid, scale_of[sid], current):
                matches.append((iss, row, sid, scale_of[sid]))
    matches = [m for m in matches if label_family(m[1]["label"]) == dominant[m[2]]]
    used_rows: set[int] = set()
    for iss, row, sid, scale in matches + forced:
        used_rows.add(id(row))
        m = mapping.setdefault(sid, {"labels": set(), "scale": scale, "issues": 0})
        m["labels"].add(row["label"][:70])
        m["issues"] += 1
        for d, (pub, dp) in row["values"].items():
            if dp == 0 and abs(pub) < 100:
                continue  # "3", "-7": too coarse to tell a revision from rounding
            published[(sid, d)].append((iss["published"], pub * scale, dp))
    prints: dict[tuple[str, str], list[tuple[str, float, int]]] = {}
    for (sid, d), pts in published.items():
        if (sid, d) not in current:
            continue  # point no longer in our data — nothing to date
        pts.sort()
        scale = mapping[sid]["scale"]
        pts = drop_unconfirmed_outliers(drop_blips(pts, scale), current[(sid, d)], scale)
        if pts:
            prints[(sid, d)] = pts
    unmatched = [(iss, row) for iss in issues for row in iss["rows"] if id(row) not in used_rows]
    return mapping, prints, unmatched


def _eq(a: tuple, b: tuple) -> bool:
    """Two prints (pub, value, dp, scale, source) show the same figure. Same scale:
    compare at the coarser printed precision (the single-source rule). Different
    scales (two sources, different units): within half a unit of the coarser print."""
    if a[3] == b[3]:
        dp = min(a[2], b[2])
        return round(a[1] / a[3], dp) == round(b[1] / b[3], dp)
    tol = 0.5 * max(10 ** -a[2] * a[3], 10 ** -b[2] * b[3])
    return abs(a[1] - b[1]) <= tol


def build_vintages_detailed(issues: list[dict], idx, current: dict[tuple[str, str], float],
                            track: date, index_sids: frozenset[str] = frozenset(),
                            explicit: dict | None = None):
    """-> (mapping, first_seen {(sid, date): pub_date},
           revisions [(sid, date, value, superseded_at, source)], unmatched [(issue, row)])

    Each source is mapped on its own (label families and units are per publication);
    the prints of every source are then merged into one chain per point."""
    by_source: dict[str, list[dict]] = defaultdict(list)
    for iss in issues:
        by_source[iss.get("source", DEFAULT_SOURCE)].append(iss)
    mapping: dict[str, dict] = {}
    merged: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    unmatched: list[tuple[dict, dict]] = []
    for src in sorted(by_source):
        m, prints, um = _source_prints(by_source[src], idx, current, index_sids, explicit)
        unmatched += um
        for sid, info in m.items():
            if sid in mapping:
                mapping[sid]["labels"] |= info["labels"]
                mapping[sid]["issues"] += info["issues"]
                mapping[sid]["sources"].add(src)
            else:
                mapping[sid] = {**info, "sources": {src}}
        for key, pts in prints.items():
            scale = m[key[0]]["scale"]
            merged[key] += [(pub, v, dp, scale, src) for pub, v, dp in pts]

    first_seen: dict[tuple[str, str], str] = {}
    revisions: list[tuple[str, str, float, str, str]] = []
    n_points: dict[str, int] = defaultdict(int)
    n_trailing_big: dict[str, int] = defaultdict(int)
    for (sid, d), prints in merged.items():
        prints.sort()
        first_seen[(sid, d)] = prints[0][0]
        chain: list[tuple] = []
        for p in prints:
            if chain and _eq(chain[-1], p):
                continue  # same figure, to the precision both printed it
            chain.append(p)
        # each printed figure was current until the next different one was published
        for a, b in zip(chain, chain[1:]):
            revisions.append((sid, d, a[1], b[0], a[4]))
        # the last printed figure vs today's value: if it has since changed we can't
        # date that change — record it as superseded when our own tracking began.
        last = chain[-1]
        cur = current[(sid, d)]
        if not _same(cur, last[1] / last[3], last[2], last[3]):
            n_trailing_big[sid] += abs(last[1] - cur) > _TRAILING_REL * max(abs(cur), 1e-9)
            # A big gap the bulletin never printed a step towards is more likely a
            # definitional difference than a revision; only small ones are recorded.
            if abs(last[1] - cur) <= _TRAILING_REL * max(abs(cur), 1e-9):
                revisions.append((sid, d, last[1], track.isoformat(), last[4]))
        n_points[sid] += 1
    # A series whose final prints often sit far from today's values is a different
    # concept that happens to coincide in most months (e.g. the bulletin's total
    # portfolio investment, incl. government Eurobonds, vs our private-only series).
    # Drop it entirely rather than record definitional gaps as revisions.
    excluded = {sid for sid in n_points if n_trailing_big[sid] > _MAX_BIG_TRAILING_SHARE * n_points[sid]}
    for sid in excluded:
        mapping[sid]["excluded"] = f"{n_trailing_big[sid]}/{n_points[sid]} final prints >15% from today"
    first_seen = {k: v for k, v in first_seen.items() if k[0] not in excluded}
    revisions = [r for r in revisions if r[0] not in excluded]
    return mapping, first_seen, revisions, unmatched


def build_vintages(issues: list[dict], idx, current: dict[tuple[str, str], float], track: date,
                   index_sids: frozenset[str] = frozenset()):
    """-> (mapping report, first_seen {(sid, date): pub_date},
           revisions [(sid, date, value, superseded_at)])"""
    mapping, first_seen, revisions, _ = build_vintages_detailed(issues, idx, current, track, index_sids)
    return mapping, first_seen, [r[:4] for r in revisions]


# ---- database side ---------------------------------------------------------------------

def load_issues(paths: list[str]) -> list[dict]:
    issues: list[dict] = []
    for p in paths:
        for iss in json.loads(Path(p).read_text(encoding="utf-8")):
            iss.setdefault("source", DEFAULT_SOURCE)
            iss.setdefault("freq", "monthly")
            issues.append(iss)
    issues.sort(key=lambda i: (i["source"], i["issue"]))
    return issues


SPEC_GLOB = "vintage_series_*.json"  # next to this file


def load_specs(paths: list[str] | None = None) -> list[dict]:
    """Series seeded from a vintage document (see `seed`): every scripts/vintage_series_*.json."""
    files = [Path(p) for p in paths] if paths else sorted(Path(__file__).parent.glob(SPEC_GLOB))
    specs: list[dict] = []
    for f in files:
        specs += json.loads(f.read_text(encoding="utf-8"))
    return specs


def explicit_map(specs: list[dict]) -> dict[tuple[str, frozenset], tuple[str, float]]:
    """{(document source, label family): (series id, scale)} for every spec label."""
    out: dict[tuple[str, frozenset], tuple[str, float]] = {}
    for sp in specs:
        for lbl in sp["labels"]:
            key = (sp.get("doc_source", DEFAULT_SOURCE), label_family(lbl))
            if key in out and out[key][0] != sp["id"]:
                raise ValueError(f"label family of {lbl!r} claimed by {out[key][0]} and {sp['id']}")
            out[key] = (sp["id"], float(sp.get("scale", 1.0)))
    return out


def latest_prints(issues: list[dict], spec: dict) -> dict[str, tuple[float, str]]:
    """{period: (value, issue)} — the most recent issue's figure for each period, over
    every row whose label family belongs to the spec."""
    fams = {label_family(lbl) for lbl in spec["labels"]}
    src = spec.get("doc_source", DEFAULT_SOURCE)
    scale = float(spec.get("scale", 1.0))
    out: dict[str, tuple[float, str]] = {}
    for iss in sorted(issues, key=lambda i: (i["published"], i["issue"])):
        if iss.get("source", DEFAULT_SOURCE) != src:
            continue
        for row in iss["rows"]:
            if label_family(row["label"]) in fams:
                for d, (v, _dp) in row["values"].items():
                    out[d] = (v * scale, iss["issue"])
    return out


def _db_inputs(issues: list[dict]):
    from app import db

    dates = sorted({d for i in issues for r in i["rows"] for d in r["values"]})
    freqs = sorted({i["freq"] for i in issues})
    obs = db.query(
        """SELECT o.series_id, o.obs_date, o.value FROM observations o
           JOIN series s ON s.id = o.series_id
           WHERE s.is_active AND lower(s.frequency) = ANY(%(f)s) AND o.dims = '{}'::jsonb
             AND o.value IS NOT NULL AND o.obs_date BETWEEN %(a)s AND %(b)s""",
        {"a": dates[0], "b": dates[-1], "f": freqs},
    )
    idx = build_index(obs)
    current = {(r["series_id"], r["obs_date"].isoformat()): float(r["value"]) for r in obs}
    index_sids = frozenset(
        r["id"] for r in db.query(
            "SELECT id FROM series WHERE unit ILIKE '%%index%%' OR name ILIKE '%%index%%' "
            "OR id LIKE 'fx.effective.%%'"
        )
    )
    return dates, freqs, obs, idx, current, index_sids


def apply(paths: list[str], commit: bool = False) -> None:
    from app import db
    from app.services.series_repo import VINTAGE_TRACKING_START

    issues = load_issues(paths)
    sources = sorted({i["source"] for i in issues})
    explicit = explicit_map(load_specs())
    dates, freqs, obs, idx, current, index_sids = _db_inputs(issues)
    print(f"{len(issues)} issues from {sources} ({freqs}), periods {dates[0]}..{dates[-1]}")
    print(f"{len(obs)} candidate observations")
    mapping, first_seen, revisions, unmatched = build_vintages_detailed(
        issues, idx, current, VINTAGE_TRACKING_START, index_sids=index_sids, explicit=explicit
    )

    excluded = sorted(sid for sid, m in mapping.items() if m.get("excluded"))
    print(f"\nmapped {len(mapping) - len(excluded)} series:")
    for sid, m in sorted(mapping.items(), key=lambda kv: -kv[1]["issues"]):
        if not m.get("excluded"):
            print(f"  {sid:60s} x{m['scale']:<7g} {m['issues']:4d} issues {','.join(sorted(m['sources']))}"
                  f"  <- {sorted(m['labels'])[0]}")
    print(f"\nexcluded {len(excluded)} series (definitional mismatch):")
    for sid in excluded:
        print(f"  {sid:60s} {mapping[sid]['excluded']}")
    print(f"\n{len(first_seen)} points dated (first print), {len(revisions)} superseded values")
    for r in revisions[:12]:
        print("  revision", r)

    def rel(r):
        cur = current[(r[0], r[1])]
        return abs(r[2] - cur) / max(abs(cur), 1e-9)
    print("\nlargest published-vs-current gaps:")
    for r in sorted(revisions, key=rel, reverse=True)[:15]:
        print(f"  {r[0]:52s} {r[1]} printed {r[2]:>12g} now {current[(r[0], r[1])]:>12g} ({rel(r):.0%}) "
              f"until {r[3]} [{r[4]}]")
    by_sid: dict[str, int] = defaultdict(int)
    for r in revisions:
        by_sid[r[0]] += 1
    print("\nrevisions per series:", dict(sorted(by_sid.items(), key=lambda kv: -kv[1])))
    earliest = min(first_seen.values()) if first_seen else None
    print(f"earliest first print: {earliest}; unmatched rows: {len(unmatched)}")
    if not commit:
        print("\ndry run - re-run with --commit to write")
        return

    existing = {r["source"] for r in db.query(
        "SELECT DISTINCT source FROM observation_revisions WHERE source IS NOT NULL")}
    missing = existing - set(sources)
    if missing:
        sys.exit(f"refusing to commit: a commit rebuilds every backfill source; also pass {sorted(missing)}")

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE observation_revisions ADD COLUMN IF NOT EXISTS source TEXT")
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {BAK_TABLE} AS "
                "SELECT series_id, obs_date, dims, first_seen_at FROM observations WHERE false"
            )
            # Full rebuild: drop every earlier backfilled revision, and put first_seen_at
            # back (from the backup) on every point an earlier run dated.
            cur.execute("DELETE FROM observation_revisions WHERE source IS NOT NULL")
            cur.execute(
                f"""UPDATE observations o SET first_seen_at = b.first_seen_at
                   FROM {BAK_TABLE} b
                   WHERE o.series_id = b.series_id AND o.obs_date = b.obs_date AND o.dims = b.dims"""
            )
            cur.executemany(
                f"""INSERT INTO {BAK_TABLE} (series_id, obs_date, dims, first_seen_at)
                   SELECT series_id, obs_date, dims, first_seen_at FROM observations
                   WHERE series_id = %s AND obs_date = %s AND dims = '{{}}'::jsonb
                     AND NOT EXISTS (SELECT 1 FROM {BAK_TABLE} b
                                     WHERE b.series_id = %s AND b.obs_date = %s)""",
                [(sid, d, sid, d) for (sid, d) in first_seen],
            )
            cur.executemany(
                """UPDATE observations SET first_seen_at = %s::date
                   WHERE series_id = %s AND obs_date = %s AND dims = '{}'::jsonb
                     AND (first_seen_at IS NULL OR first_seen_at > %s::date)""",
                [(p, sid, d, p) for (sid, d), p in first_seen.items()],
            )
            cur.executemany(
                """INSERT INTO observation_revisions (series_id, obs_date, dims, value, superseded_at, source)
                   VALUES (%s, %s, '{}'::jsonb, %s, %s::date, %s)""",
                [(sid, d, v, sup, src) for sid, d, v, sup, src in revisions],
            )
    print(f"\ncommitted: {len(first_seen)} first-seen dates, {len(revisions)} revisions (sources={sources})")


# ---- seed: new series from a vintage document -------------------------------------------

def seed(paths: list[str], commit: bool = False, only: set[str] | None = None) -> None:
    """Create/refresh the series declared in scripts/vintage_series_*.json from the
    documents' prints: each period's value is the LATEST issue's figure. Run `apply`
    afterwards — it dates every point's first print and records the earlier prints as
    revisions (the rows are mapped explicitly, by label family)."""
    from app import db

    issues = load_issues(paths)
    specs = [s for s in load_specs() if not only or s["id"] in only]
    plan = []
    for sp in specs:
        pts = latest_prints(issues, sp)
        if not pts:
            print(f"  {sp['id']:48s} no rows in these documents")
            continue
        lo, hi = sp.get("min_value"), sp.get("max_value")
        bad = {d: v for d, (v, _i) in pts.items()
               if (lo is not None and v < lo) or (hi is not None and v > hi)}
        dates = sorted(pts)
        print(f"  {sp['id']:48s} {len(pts):4d} pts {dates[0]}..{dates[-1]} latest {pts[dates[-1]][0]:g}"
              f"{f'  OUT OF BOUNDS {len(bad)}: ' + str(sorted(bad.items())[:3]) if bad else ''}")
        plan.append((sp, {d: v for d, (v, _i) in pts.items() if d not in bad}))
    if not commit:
        print("\ndry run - re-run with --commit to write")
        return
    with db.connection() as conn:
        with conn.cursor() as cur:
            for sp, pts in plan:
                cur.execute(
                    """INSERT INTO series (id, module, name, description, unit, frequency, source, source_url,
                                           dimensions, min_value, max_value, tier, is_active)
                       VALUES (%(id)s, %(module)s, %(name)s, %(description)s, %(unit)s, %(frequency)s,
                               %(source)s, %(source_url)s, '{}', %(min_value)s, %(max_value)s, 'basic', true)
                       ON CONFLICT (id) DO UPDATE SET module = EXCLUDED.module, name = EXCLUDED.name,
                           description = EXCLUDED.description, unit = EXCLUDED.unit,
                           frequency = EXCLUDED.frequency, source_url = EXCLUDED.source_url,
                           min_value = EXCLUDED.min_value, max_value = EXCLUDED.max_value, is_active = true""",
                    {"source": "SBP", "source_url": None, "min_value": None, "max_value": None, **sp},
                )
                cur.executemany(
                    """INSERT INTO observations (series_id, obs_date, value, dims, flagged, revised_at)
                       VALUES (%s, %s, %s, '{}'::jsonb, false, now())
                       ON CONFLICT (series_id, obs_date, dims) DO UPDATE SET value = EXCLUDED.value
                       WHERE observations.value IS DISTINCT FROM EXCLUDED.value""",
                    [(sp["id"], d, v) for d, v in sorted(pts.items())],
                )
                cur.execute(
                    "UPDATE series SET first_date = (SELECT min(obs_date) FROM observations WHERE series_id = %s), "
                    "last_date = (SELECT max(obs_date) FROM observations WHERE series_id = %s) WHERE id = %s",
                    (sp["id"], sp["id"], sp["id"]),
                )
    print(f"\nseeded {len(plan)} series — now run `apply` (all sources) to date their prints")


# ---- candidates: figures we don't serve yet ---------------------------------------------

_STOP = {"of", "the", "and", "in", "on", "to", "by", "for", "net", "total", "end", "month", "average"}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2 and w not in _STOP}


def summarize_unmatched(unmatched: list[tuple[dict, dict]]) -> list[dict]:
    """Group unmatched rows by (source, label family) -> one candidate each."""
    groups: dict[tuple[str, frozenset], dict] = {}
    for iss, row in unmatched:
        key = (iss.get("source", DEFAULT_SOURCE), label_family(row["label"]))
        g = groups.setdefault(key, {"source": key[0], "label": row["label"], "issues": 0,
                                    "periods": set(), "first_issue": iss["issue"], "last_issue": iss["issue"],
                                    "freq": iss.get("freq", "monthly"), "sample": {}})
        g["issues"] += 1
        g["periods"] |= set(row["values"])
        g["last_issue"] = max(g["last_issue"], iss["issue"])
        g["first_issue"] = min(g["first_issue"], iss["issue"])
        if iss["issue"] >= g["last_issue"]:
            g["label"] = row["label"]
            g["sample"] = row["values"]
    out = []
    for g in groups.values():
        periods = sorted(g["periods"])
        last = sorted(g["sample"].items())[-3:]
        out.append({
            "source": g["source"], "label": g["label"].strip(), "freq": g["freq"], "issues": g["issues"],
            "first_issue": g["first_issue"], "last_issue": g["last_issue"],
            "periods": len(periods), "from": periods[0] if periods else "", "to": periods[-1] if periods else "",
            "latest_values": "; ".join(f"{d}={v[0]}" for d, v in last),
        })
    return sorted(out, key=lambda c: (c["source"], -c["issues"]))


def candidates(paths: list[str], out_csv: str) -> None:
    from app import db
    from app.services.series_repo import VINTAGE_TRACKING_START

    issues = load_issues(paths)
    _dates, _freqs, _obs, idx, current, index_sids = _db_inputs(issues)
    _mapping, _fs, _revs, unmatched = build_vintages_detailed(
        issues, idx, current, VINTAGE_TRACKING_START, index_sids=index_sids,
        explicit=explicit_map(load_specs()))
    cands = summarize_unmatched(unmatched)
    catalog = [(r["id"], r["name"], _words(r["name"] + " " + r["id"].replace(".", " ").replace("_", " ")))
               for r in db.query("SELECT id, name FROM series WHERE is_active")]
    for c in cands:
        w = _words(c["label"])
        best = sorted(catalog, key=lambda t: -len(w & t[2]))[:3]
        c["nearest_existing"] = " | ".join(f"{sid} ({name[:50]})" for sid, name, ww in best if w & ww)
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(cands[0]) if cands else ["source"])
        wr.writeheader()
        wr.writerows(cands)
    print(f"{len(cands)} candidate row families -> {out_csv}")
    for c in cands[:60]:
        print(f"  [{c['source']}] {c['issues']:4d} issues {c['from']}..{c['to']}  {c['label'][:70]}"
              f"   ~ {c['nearest_existing'][:90]}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    if cmd == "apply":
        apply(args, commit="--commit" in sys.argv)
    elif cmd == "candidates":
        candidates(args[:-1], args[-1])
    elif cmd == "seed":
        only = {a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")}
        seed(args, commit="--commit" in sys.argv, only=only or None)
    else:
        print(__doc__)
