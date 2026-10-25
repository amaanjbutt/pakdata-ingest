"""Give series that share a name a name of their own (data-quality audit, 2026-10-14).

About 700 names were shared by ~1,500 active series: the monthly and the quarterly version of
one BOP item, the same aggregate from the monetary survey and from the reserve-money table,
successive survey vintages (1964–79, 1981–2008, …). On the site they look like duplicates
whose numbers disagree. Each colliding group gets the smallest qualifier that tells its
members apart, in this order:

  1. frequency            "… (quarterly)" / "… (weekly)"         — when members differ by it
  2. years covered        "… (1964–1979)"                       — successive vintages
  3. SBP table            "… (Reserve Money RM)"                — easydata_datasets (filled by dq_audit)
  4. unit                 "… (USD million)"
  5. SBP's own series name, where the composed name dropped the detail that tells them apart
  6. SBP's series code — last resort for series one SBP table gives the same leaf name (principal /
     interest lines of a debt table); the hierarchy that tells them apart isn't in the data API

Names that end in a bare "of" (the base year was clipped: "GDP at Constant Basic Prices of") are
completed from the table name ("… of 2015-16") first. Groups still identical after every step are
listed for review, not renamed. Names are composed
by `discover_easydata.compose_name` and refreshed through a manual staging swap
(scripts/refresh_easydata_metadata.py) — re-run this script after any such swap.

  docker compose -f docker-compose.server.yml exec -T api python -m scripts.disambiguate_series_names [--commit]
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import date

FREQ_LABEL = {"daily": "daily", "weekly": "weekly", "monthly": "monthly", "quarterly": "quarterly",
              "half_yearly": "half-yearly", "annual": "annual"}
UNIT_LABEL = {"usd": "USD", "usd_th": "USD thousand", "usd_mn": "USD million", "pkr": "PKR",
              "pkr_mn": "PKR million", "pkr_bn": "PKR billion", "pkr_tn": "PKR trillion",
              "percent": "percent", "index": "index", "count": "count", "million": "million"}


def _years(r: dict) -> str:
    y0 = r["first_date"].year if r["first_date"] else None
    y1 = r["last_date"].year if r["last_date"] else None
    if y0 is None:
        return ""
    current = r["last_date"] and (date.today() - r["last_date"]).days < 550
    return f"{y0}–" if current else (f"{y0}" if y0 == y1 else f"{y0}–{y1}")


_FREQ_WORDS = re.compile(r"^(daily|weekly|monthly|quarterly|half[- ]yearly|annual|yearly)\s+", re.I)


def _table(r: dict, tables: dict[str, str]) -> str:
    """The SBP table name, minus a leading frequency word (frequency is step 1). Pure."""
    return _FREQ_WORDS.sub("", tables.get(r["ds"] or "", "")).strip()


def complete_clipped(name: str, table: str) -> str:
    """'Services — GDP at Constant Basic Prices of' + table '... of 2015-16' -> '... of 2015-16'. Pure."""
    m = re.search(r" of (\S+)$", table or "")
    if not m:
        return name
    return re.sub(r" of( —|$)", lambda g: f" of {m.group(1)}" + (g.group(1) if g.group(1) else ""), name, count=1)


def qualify(group: list[dict], tables: dict[str, str], raw: dict[str, str] | None = None,
            codes: dict[str, str] | None = None) -> dict[str, str]:
    """{series_id: qualifier} for one group of series sharing a name. Pure.

    Applies the qualifier steps in order, each only to sub-groups still colliding, and
    combines them ("quarterly, 2005–"). Members left identical get no qualifier."""
    raw, codes = raw or {}, codes or {}
    quals: dict[str, list[str]] = {r["id"]: [] for r in group}

    def step(members: list[dict], key) -> list[list[dict]]:
        vals = {r["id"]: key(r) for r in members}
        if len(set(vals.values())) <= 1 or any(not v for v in vals.values()):
            return [members]
        by: dict[str, list[dict]] = defaultdict(list)
        for r in members:
            quals[r["id"]].append(vals[r["id"]])
            by[vals[r["id"]]].append(r)
        return list(by.values())

    pending = [group]
    for key in (lambda r: FREQ_LABEL.get(r["frequency"], ""),
                _years,
                lambda r: _table(r, tables),
                lambda r: UNIT_LABEL.get(r["unit"] or "", r["unit"] or ""),
                lambda r: raw.get(r["id"], ""),
                lambda r: f"SBP series {codes[r['id']]}" if r["id"] in codes else ""):
        nxt = []
        for members in pending:
            if len(members) < 2:
                continue
            nxt += step(members, key)
        pending = nxt
    return {sid: ", ".join(q) for sid, q in quals.items() if q}


def main(commit: bool) -> None:
    from app import db
    from ingestion.easydata_config import load_series

    try:
        tables = {r["code"]: r["name"] for r in db.query("SELECT code, name FROM easydata_datasets")}
        by_key = {r["easydata_key"]: r["name"] for r in db.query("SELECT easydata_key, name FROM easydata_series_names")}
    except Exception:  # noqa: BLE001 — dq_audit fills these tables as it reconciles
        tables, by_key = {}, {}
    cfg = load_series()
    raw = {s.id: by_key[s.easydata_key] for s in cfg if s.easydata_key in by_key}
    codes = {s.id: s.easydata_key.split(".", 1)[-1] for s in cfg}

    rows = db.query("SELECT id, name, frequency, unit, easydata_dataset_code ds, first_date, last_date "
                    "FROM series WHERE is_active ORDER BY name, id")
    # 0. complete names whose base year was clipped ("… Prices of")
    current = {}
    for r in rows:
        current[r["id"]] = complete_clipped(r["name"], tables.get(r["ds"] or "", "")) if re.search(r" of( —|$)", r["name"]) else r["name"]
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[current[r["id"]]].append(r)
    renames, unresolved = [], []
    for name, members in groups.items():
        if len(members) == 1:
            r = members[0]
            if name != r["name"]:
                renames.append((name, r["id"]))
            continue
        q = qualify(members, tables, raw, codes)
        new = {r["id"]: f"{name} ({q[r['id']]})" if r["id"] in q else name for r in members}
        if len(set(new.values())) < len(members):
            unresolved.append(name)
        renames += [(n, r["id"]) for r in members if (n := new[r["id"]]) != r["name"]]
    shared = sum(1 for m in groups.values() if len(m) > 1)
    print(f"{shared} shared names; {len(renames)} renames; {len(unresolved)} names still shared after every qualifier")
    for n, sid in renames[:25]:
        print(f"  {sid}: {n}")
    for name in unresolved[:15]:
        print(f"  unresolved: {name}")
    if not commit:
        print("dry run — re-run with --commit to write (backup table first)")
        return
    tag = date.today().strftime("%Y%m%d")
    db.execute(f"CREATE TABLE IF NOT EXISTS series_names_bak_{tag} AS SELECT id, name FROM series WHERE false")
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(f"INSERT INTO series_names_bak_{tag} SELECT id, name FROM series WHERE id = ANY(%s)",
                    ([sid for _, sid in renames],))
        cur.executemany("UPDATE series SET name = %s WHERE id = %s", renames)
    print(f"renamed {len(renames)} series (backup: series_names_bak_{tag})")


if __name__ == "__main__":
    main("--commit" in sys.argv)
