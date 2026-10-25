"""Load `pbs_trade_hs` rows (exports/imports by product x country) into prod.

The customs volumes are big (~2.5M rows for FY1991 onward) and PBS blocks the VPS, so they are
parsed on a workstation into a gzipped CSV and copied in here (inside the api container, so the
database credentials never leave the box):

  # workstation (local api container; DIR holds the downloaded volumes):
  python -m scripts.load_trade_hs parse DIR trade_hs.csv.gz
  # VPS:
  docker compose -f docker-compose.server.yml cp trade_hs.csv.gz api:/tmp/
  docker compose -f docker-compose.server.yml exec -T api python -m scripts.load_trade_hs load /tmp/trade_hs.csv.gz [--commit]
  # recompute the readable names (ingestion/trade_names) for every stored code:
  docker compose -f docker-compose.server.yml exec -T api python -m scripts.load_trade_hs names [--commit]

Files are applied in publication order, so a later file's (revised) figure for a year wins.
"""
from __future__ import annotations

import csv
import gzip
import io
import os
import re
import sys


def pub_key(name: str) -> tuple:
    """Sort key ≈ publication order: the latest fiscal year a file name mentions. Pure."""
    n = name.replace("204-15", "2014-15")
    ys = [int(y) for y in re.findall(r"(?:19|20)\d\d", n)]
    short = re.search(r"(?:Export|Import)-?0?6(\d\d)\b", n, re.I)  # D-10_Export-0625 / Export0624
    if short:
        ys.append(2000 + int(short.group(1)))
    return (max(ys) if ys else 0, name)


def _add(a, b):
    return b if a is None else a if b is None else a + b


def parse_dir(src: str, out: str) -> None:
    from ingestion.jobs.pbs_trade_hs import classification, parse_file, reconcile

    rows: dict[tuple, tuple] = {}
    meta: dict[str, list] = {}
    for name in sorted(os.listdir(src), key=pub_key):
        r, m = parse_file(name, open(os.path.join(src, name), "rb").read())
        bad = reconcile(r)
        print(f"{name}: {len(r)} rows, {len(m)} codes, {len(bad)} unreconciled", flush=True)
        per_file: dict[tuple, tuple] = {}
        for x in r:  # two old spellings of one partner in the same file add up
            k = (x.flow, x.code, x.fy, x.country)
            if k in per_file:
                q0, v0 = per_file[k]
                per_file[k] = (_add(q0, x.quantity), _add(v0, x.value_pkr_th))
            else:
                per_file[k] = (x.quantity, x.value_pkr_th)
        rows.update(per_file)  # a later file's (revised) figure for a year wins
        fys = sorted({x.fy for x in r}) or [0]
        for code, cm in m.items():
            e = meta.get(code)
            if e is None:
                meta[code] = [classification(code), cm.description, cm.unit, fys[0], fys[-1]]
            else:  # latest wording wins; widen the year span
                e[1], e[2] = cm.description or e[1], cm.unit or e[2]
                e[3], e[4] = min(e[3], fys[0]), max(e[4], fys[-1])
    with gzip.open(out, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#codes"])
        for code, (cl, d, u, a, b) in sorted(meta.items()):
            w.writerow([code, cl, d, u, a, b])
        w.writerow(["#rows"])
        for (fl, code, fy, cty), (q, v) in rows.items():
            w.writerow([fl, code, fy, cty, "" if q is None else q, "" if v is None else v])
    print(f"wrote {len(rows)} rows, {len(meta)} codes to {out}")


def upsert_rows(rows, metas) -> int:
    """Rows/metas from the job itself (TradeHsRow / CodeMeta) → COPY + upsert."""
    from ingestion.jobs.pbs_trade_hs import classification

    fys = sorted({r.fy for r in rows}) or [None]
    codes = [(m.code, classification(m.code), m.description, m.unit, fys[0], fys[-1]) for m in metas]
    data = [(r.flow, r.code, r.fy, r.country, r.quantity, r.value_pkr_th) for r in rows]
    return copy_in(codes, data)


def _csv(rows) -> str:
    b = io.StringIO()
    w = csv.writer(b)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return b.getvalue()


_CODE_COLS = ("code", "classification", "description", "unit", "first_fy", "last_fy",
              "display_name", "short_name", "hs_heading", "unit_clean")


def with_names(codes) -> list[tuple]:
    """Append ingestion/trade_names' readable fields to (code, cl, desc, unit, fy0, fy1) rows."""
    from ingestion.trade_names import REF_DIR, clean, reference_available

    if not reference_available():
        raise SystemExit(f"HS/SITC reference files missing in {REF_DIR} (data/reference/H*.json, S3.json)")
    out = []
    for c in codes:
        n = clean(c[0], c[1], c[2], c[3])
        out.append(tuple(c) + (n["display_name"], n["short_name"], n["hs_heading"], n["unit_clean"]))
    return out


def copy_in(codes, data, chunk: int = 50_000) -> int:
    """Upsert codes then rows. `data` may be any iterable (streamed in chunks, so a 3M-row load
    never sits in memory at once)."""
    from app import db

    cols = ", ".join(_CODE_COLS)
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _hs (LIKE trade_hs) ON COMMIT DROP")
            cur.execute(f"CREATE TEMP TABLE _hc ON COMMIT DROP AS SELECT {cols} FROM trade_hs_codes LIMIT 0")
            with cur.copy(f"COPY _hc ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')") as cp:
                cp.write(_csv(with_names(codes)))
            with cur.copy("COPY _hs (flow, code, fy, country, quantity, value_pkr_th) FROM STDIN "
                          "WITH (FORMAT csv, NULL '')") as cp:
                buf: list = []
                for r in data:
                    buf.append(r)
                    if len(buf) >= chunk:
                        cp.write(_csv(buf))
                        buf = []
                if buf:
                    cp.write(_csv(buf))
            cur.execute(f"""INSERT INTO trade_hs_codes ({cols}) SELECT {cols} FROM _hc
                           ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description,
                             unit = COALESCE(NULLIF(EXCLUDED.unit, ''), trade_hs_codes.unit),
                             classification = EXCLUDED.classification,
                             first_fy = LEAST(trade_hs_codes.first_fy, EXCLUDED.first_fy),
                             last_fy = GREATEST(trade_hs_codes.last_fy, EXCLUDED.last_fy),
                             display_name = EXCLUDED.display_name, short_name = EXCLUDED.short_name,
                             hs_heading = EXCLUDED.hs_heading,
                             unit_clean = COALESCE(EXCLUDED.unit_clean, trade_hs_codes.unit_clean)""")
            cur.execute("""INSERT INTO trade_hs SELECT * FROM _hs
                           ON CONFLICT (flow, code, fy, country) DO UPDATE SET
                             quantity = EXCLUDED.quantity, value_pkr_th = EXCLUDED.value_pkr_th
                           WHERE (trade_hs.quantity, trade_hs.value_pkr_th)
                                 IS DISTINCT FROM (EXCLUDED.quantity, EXCLUDED.value_pkr_th)""")
            return cur.rowcount


def names(commit: bool, sample: int = 25) -> None:
    """Recompute display_name / short_name / hs_heading / unit_clean for every stored code."""
    from app import db

    rows = db.query("SELECT code, classification, description, unit, first_fy, last_fy, display_name "
                    "FROM trade_hs_codes ORDER BY code")
    fresh = with_names([(r["code"], r["classification"], r["description"], r["unit"],
                         r["first_fy"], r["last_fy"]) for r in rows])
    changed = [f for f, r in zip(fresh, rows) if f[6] != r["display_name"]]
    print(f"{len(rows)} codes, {len(changed)} display names change")
    for f in changed[:sample]:
        print(f"  {f[0]:9} {f[2][:34]:34} -> {f[6][:80]}")
    if not commit:
        print("dry run - re-run with --commit to write")
        return
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE _nm (code text, display_name text, short_name text, "
                        "hs_heading text, unit_clean text) ON COMMIT DROP")
            with cur.copy("COPY _nm FROM STDIN WITH (FORMAT csv, NULL '')") as cp:
                cp.write(_csv([(f[0], f[6], f[7], f[8], f[9]) for f in fresh]))
            cur.execute("""UPDATE trade_hs_codes c SET display_name = n.display_name,
                             short_name = n.short_name, hs_heading = n.hs_heading,
                             unit_clean = n.unit_clean
                           FROM _nm n WHERE n.code = c.code""")
            print(f"updated {cur.rowcount} codes")


def _read(path: str):
    """(codes, row iterator) from a parse_dir CSV."""
    codes = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.reader(f):
            if r and r[0] == "#rows":
                break
            if r and not r[0].startswith("#"):
                codes.append((r[0], r[1], r[2], r[3] or None, int(r[4]), int(r[5])))

    def rows():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            started = False
            for r in csv.reader(f):
                if r and r[0] == "#rows":
                    started = True
                    continue
                if started and r:
                    yield (r[0], r[1], int(r[2]), r[3], float(r[4]) if r[4] else None,
                           float(r[5]) if r[5] else None)
    return codes, rows


def load(path: str, commit: bool) -> None:
    codes, rows = _read(path)
    n, fys = 0, set()
    for r in rows():
        n += 1
        fys.add(r[2])
    print(f"{n} rows, {len(codes)} codes, fiscal years {min(fys)}-{max(fys)}")
    if not commit:
        print("dry run - re-run with --commit to write")
        return
    from ingestion.jobs.pbs_trade_hs import PbsTradeHsJob

    job = PbsTradeHsJob()
    run_id = job._start_run()
    try:
        written = copy_in(codes, rows())
        job._finish(run_id, "success", written, f"loaded from {os.path.basename(path)}", None)
        print(f"upserted {written} rows")
    except Exception as exc:
        job._finish(run_id, "failed", 0, str(exc)[:500], None)
        raise


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) == 3 and a[0] == "parse":
        parse_dir(a[1], a[2])
    elif len(a) == 2 and a[0] == "load":
        load(a[1], "--commit" in sys.argv)
    elif len(a) == 1 and a[0] == "names":
        names("--commit" in sys.argv)
    else:
        print(__doc__)
        sys.exit(1)
