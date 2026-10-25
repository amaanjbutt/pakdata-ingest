"""One-off AMIS history backfill — run on a workstation, load on the VPS.

  python -m scripts.amis_backfill fqp OUT_DIR [--workers 3]      # FQP history 2007-05-07 -> today
  python -m scripts.amis_backfill minmax OUT_DIR [--from YYYY-MM-DD] [--workers 2]
                                                                  # min/max/FQP/quantity per market-day
  python -m scripts.amis_backfill pack OUT_DIR OUT.jsonl.gz       # rows + reference for the loader
  # VPS: python -m scripts.amis_backfill load /tmp/OUT.jsonl.gz [--commit]   (inside the api container)

`fqp` uses the Price_Trends report: one CSV export per commodity x market covers the whole
history (~19k pairs, a few hours). `minmax` walks ViewPrices one market-day at a time
(~420k pages since 2007 — days of polite fetching); only days AMIS reports as having data
(CitiesPerformance) are requested. Both are resumable: each pair / market-day is one file
under OUT_DIR, skipped when present.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from ingestion.jobs.amis_prices import DATA_START, AmisClient, clean_name

_lock = threading.Lock()


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_name(name).lower())


def reference(out: Path) -> dict:
    ref_file = out / "reference.json"
    if ref_file.exists():
        return json.loads(ref_file.read_text(encoding="utf-8"))
    cl = AmisClient()
    markets = cl.markets()
    commodities = cl.commodities()
    crops, report_markets = cl.trend_options()
    by_name = {_norm(n): i for i, n in commodities.items()}
    crop_map = {c: by_name[_norm(n)] for c, n in crops.items() if _norm(n) in by_name}
    m_by_name = {_norm(n): i for i, n in markets.items()}
    market_map = {r: m_by_name[_norm(n)] for r, n in report_markets.items() if _norm(n) in m_by_name}
    # categories from a couple of large markets' pages (Grains / Vegetables / Fruits)
    cats: dict[int, str] = {}
    for mid in (1, 2):  # Lahore, Faisalabad
        _d, rows = cl.market_day(mid)
        cats.update({r["commodity_id"]: r["category"] for r in rows if r["category"]})
    ref = {"markets": {str(k): v for k, v in markets.items()},
           "commodities": {str(k): [v, cats.get(k)] for k, v in commodities.items()},
           "crop_map": {str(k): v for k, v in crop_map.items()},
           "market_map": {str(k): v for k, v in market_map.items()}}
    out.mkdir(parents=True, exist_ok=True)
    ref_file.write_text(json.dumps(ref, ensure_ascii=False, indent=1), encoding="utf-8")
    unmapped = [n for c, n in crops.items() if c not in crop_map]
    print(f"{len(markets)} markets ({len(market_map)} report markets mapped), {len(commodities)} commodities, "
          f"{len(crop_map)} report crops mapped"
          f"{'; unmapped: ' + str(unmapped[:10]) if unmapped else ''}")
    return ref


def fqp(out: Path, workers: int = 3) -> None:
    ref = reference(out)
    d = out / "fqp"
    d.mkdir(parents=True, exist_ok=True)
    pairs = [(int(c), int(cid), int(rm), int(mid)) for c, cid in ref["crop_map"].items()
             for rm, mid in ref["market_map"].items()]
    todo = [p for p in pairs if not (d / f"{p[1]}_{p[3]}.json").exists()]
    print(f"{len(pairs)} commodity x market pairs, {len(todo)} to fetch")
    end = date.today()
    local = threading.local()
    done = [0, 0]

    def work(p):
        crop, cid, report_mid, mid = p   # report ids in, site ids stored
        cl = getattr(local, "cl", None) or AmisClient(delay=0.3)
        local.cl = cl
        for attempt in range(3):
            try:
                pts = cl.trends(crop, report_mid, DATA_START, end)
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    print(f"  fail {cid}x{mid}: {exc}")
                    return
                local.cl = cl = AmisClient(delay=0.3)
        (d / f"{cid}_{mid}.json").write_text(json.dumps([[x.isoformat(), v] for x, v in pts]))
        with _lock:
            done[0] += 1
            done[1] += len(pts)
            if done[0] % 250 == 0:
                print(f"  {done[0]}/{len(todo)} pairs, {done[1]} prices", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(work, todo))
    print(f"fqp done: {done[0]} pairs fetched, {done[1]} prices")


def minmax(out: Path, start: date, workers: int = 2) -> None:
    ref = reference(out)
    d = out / "minmax"
    d.mkdir(parents=True, exist_ok=True)
    cov = out / "coverage.csv"   # date,markets_reporting — from CitiesPerformance (see HANDOFF)
    days = [date.fromisoformat(x.split(",")[0]) for x in cov.read_text().split()] if cov.exists() else []
    days = [x for x in days if x >= start]
    todo = [(int(m), x) for m in ref["markets"] for x in days if not (d / m / f"{x}.json").exists()]
    print(f"{len(todo)} market-days to fetch")
    local = threading.local()
    n = [0]

    def work(item):
        mid, day = item
        cl = getattr(local, "cl", None) or AmisClient(delay=0.5)
        local.cl = cl
        try:
            _w, rows = cl.market_day(mid, day)
        except Exception:  # noqa: BLE001
            local.cl = None
            return
        (d / str(mid)).mkdir(exist_ok=True)
        (d / str(mid) / f"{day}.json").write_text(json.dumps(rows))
        with _lock:
            n[0] += 1
            if n[0] % 500 == 0:
                print(f"  {n[0]}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(work, todo))


def pack(out: Path, dest: str) -> None:
    ref = reference(out)
    rows = 0
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"reference": ref}, ensure_ascii=False) + "\n")
        for f in sorted((out / "fqp").glob("*.json")):
            cid, mid = map(int, f.stem.split("_"))
            for day, v in json.loads(f.read_text()):
                fh.write(json.dumps([cid, mid, day, None, None, v, None]) + "\n")
                rows += 1
        for f in sorted((out / "minmax").glob("*/*.json")) if (out / "minmax").exists() else []:
            day = f.stem
            for r in json.loads(f.read_text()):
                fh.write(json.dumps([r["commodity_id"], r["market_id"], day, r["min"], r["max"], r["fqp"],
                                     r["quantity"]]) + "\n")
                rows += 1
    print(f"packed {rows} rows -> {dest}")


def load(src: str, commit: bool = False, chunk: int = 200_000) -> None:
    from ingestion.jobs.amis_prices import upsert_prices, upsert_reference

    with gzip.open(src, "rt", encoding="utf-8") as fh:
        ref = json.loads(fh.readline())["reference"]
        markets = {int(k): v for k, v in ref["markets"].items()}
        commodities = {int(k): (v[0], v[1]) for k, v in ref["commodities"].items()}
        print(f"{len(markets)} markets, {len(commodities)} commodities")
        if commit:
            upsert_reference(markets, commodities)
        batch, total, written = [], 0, 0
        for line in fh:
            c, m, d, mn, mx, fqp, q = json.loads(line)
            batch.append({"commodity_id": c, "market_id": m, "obs_date": d, "min": mn, "max": mx, "fqp": fqp,
                          "quantity": q})
            if len(batch) >= chunk:
                total += len(batch)
                written += upsert_prices(batch) if commit else 0
                batch = []
                print(f"  {total} rows read, {written} written", flush=True)
        total += len(batch)
        written += upsert_prices(batch) if commit and batch else 0
    print(f"{total} rows read, {written} written{'' if commit else ' (dry run — add --commit)'}")


if __name__ == "__main__":
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    opts = {x.split("=")[0]: (x.split("=") + [""])[1] for x in sys.argv[1:] if x.startswith("--")}
    cmd = a[0] if a else ""
    if cmd == "fqp":
        fqp(Path(a[1]), int(opts.get("--workers") or 3))
    elif cmd == "minmax":
        minmax(Path(a[1]), date.fromisoformat(opts.get("--from") or DATA_START.isoformat()),
               int(opts.get("--workers") or 2))
    elif cmd == "pack":
        pack(Path(a[1]), a[2])
    elif cmd == "load":
        load(a[1], commit="--commit" in opts)
    else:
        print(__doc__)
