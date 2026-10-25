"""Query layer over the series catalog and observations store.

Keeps SQL in one place; everything is parameterized.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app import db


def _catalog_clauses(
    module: str | None, source: str | None, q: str | None, canonical_only: bool
) -> tuple[list[str], dict[str, Any]]:
    clauses = ["is_active = true"]
    params: dict[str, Any] = {}
    if module:
        clauses.append("module = %(module)s")
        params["module"] = module
    if source:
        clauses.append("source = %(source)s")
        params["source"] = source
    if q:
        clauses.append("(name ILIKE %(q)s OR description ILIKE %(q)s OR id ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    if canonical_only:
        # Skip label-duplicates (identical name+description twins) so hub listings
        # link one canonical page per series, not the noindexed copies.
        clauses.append("canonical_id IS NULL")
    return clauses, params


def list_series(
    module: str | None = None, source: str | None = None, q: str | None = None,
    limit: int | None = None, offset: int | None = None, canonical_only: bool = False,
) -> list[dict]:
    clauses, params = _catalog_clauses(module, source, q, canonical_only)
    sql = f"SELECT * FROM series WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    if offset:
        sql += " OFFSET %(offset)s"
        params["offset"] = offset
    return db.query(sql, params)


def count_series(module: str | None = None, source: str | None = None,
                 q: str | None = None, canonical_only: bool = False) -> int:
    clauses, params = _catalog_clauses(module, source, q, canonical_only)
    row = db.query_one(f"SELECT count(*) AS c FROM series WHERE {' AND '.join(clauses)}", params)
    return int(row["c"]) if row else 0


def get_series(series_id: str) -> dict | None:
    return db.query_one("SELECT * FROM series WHERE id = %(id)s", {"id": series_id})


def get_notes(series_id: str, dates: list[date]) -> list[dict]:
    """Public notes on the given points (observation_notes): values confirmed against the
    publisher but worth a caveat. Empty list when the table doesn't exist yet."""
    if not dates:
        return []
    try:
        rows = db.query(
            "SELECT obs_date, dims, note, kind FROM observation_notes "
            "WHERE series_id = %(sid)s AND obs_date = ANY(%(d)s) ORDER BY obs_date",
            {"sid": series_id, "d": list(set(dates))},
        )
    except Exception:  # noqa: BLE001 — a missing table must never break series reads
        return []
    return [{"date": r["obs_date"], "dims": r["dims"] or {}, "note": r["note"], "kind": r["kind"]} for r in rows]


def get_observations(
    series_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    dims: dict[str, str] | None = None,
    limit: int = 1000,
    sort: str = "desc",
) -> list[dict]:
    clauses = ["series_id = %(sid)s"]
    params: dict[str, Any] = {"sid": series_id, "limit": limit}
    if date_from:
        clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    if dims:
        # dims is a subset match against the JSONB column.
        clauses.append("dims @> %(dims)s::jsonb")
        import json

        params["dims"] = json.dumps(dims)
    order = "ASC" if sort == "asc" else "DESC"
    sql = (
        f"SELECT obs_date, value, dims, flagged FROM observations "
        f"WHERE {' AND '.join(clauses)} "
        f"ORDER BY obs_date {order} LIMIT %(limit)s"
    )
    return db.query(sql, params)


def get_observations_asof(
    series_id: str,
    vintage: date,
    date_from: date | None = None,
    date_to: date | None = None,
    dims: dict[str, str] | None = None,
    limit: int = 1000,
    sort: str = "desc",
) -> list[dict]:
    """Observations as they were known at the END of `vintage` (a revision
    vintage). For each (obs_date, dims) it returns the current value if it was
    already in effect by then, else the prior value from `observation_revisions`
    that was still current on that date. Points not yet recorded as-of `vintage`
    are omitted. See the trigger in schema.sql.
    """
    import json

    params: dict[str, Any] = {"sid": series_id, "limit": limit, "vt": vintage}
    date_dim_clauses = []
    if date_from:
        date_dim_clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        date_dim_clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    if dims:
        date_dim_clauses.append("dims @> %(dims)s::jsonb")
        params["dims"] = json.dumps(dims)
    extra = ("" if not date_dim_clauses else " AND " + " AND ".join(date_dim_clauses))
    order = "ASC" if sort == "asc" else "DESC"
    params["track"] = VINTAGE_TRACKING_START
    # vintage_ts = midnight after `vintage`. A point is in the vintage if we held it
    # by then (known_since < ts); its value is the earliest revision superseded at or
    # after ts (what was current on `vintage`), else today's value.
    #
    # known_since = first_seen_at when recorded (rows since 2026-10-04). Older rows
    # predate that column and their revised_at was bumped by every re-ingest, so use
    # the earliest evidence we have: the first revision, capped at the tracking start
    # for points dated before it (we held them by then). Biased to omit, never to
    # show a value earlier than we had it.
    sql = f"""
        WITH vt AS (SELECT (%(vt)s::date + INTERVAL '1 day') AS ts),
        first_rev AS (
            SELECT obs_date, dims, min(superseded_at) AS first_sup
            FROM observation_revisions
            WHERE series_id = %(sid)s{extra}
            GROUP BY obs_date, dims
        ),
        cur AS (
            SELECT o.obs_date, o.dims, o.value, o.flagged,
                   COALESCE(
                       o.first_seen_at,
                       CASE WHEN o.obs_date < %(track)s::date
                            THEN LEAST(o.revised_at, COALESCE(f.first_sup, o.revised_at), %(track)s::timestamptz)
                            ELSE LEAST(o.revised_at, COALESCE(f.first_sup, o.revised_at))
                       END
                   ) AS known_since
            FROM observations o
            LEFT JOIN first_rev f ON f.obs_date = o.obs_date AND f.dims = o.dims
            WHERE o.series_id = %(sid)s{extra.replace("obs_date", "o.obs_date").replace("dims @>", "o.dims @>")}
        ),
        rev AS (
            SELECT DISTINCT ON (obs_date, dims) obs_date, dims, value
            FROM observation_revisions
            WHERE series_id = %(sid)s
              AND superseded_at >= (SELECT ts FROM vt){extra}
            ORDER BY obs_date, dims, superseded_at ASC
        )
        SELECT c.obs_date,
               CASE WHEN r.obs_date IS NOT NULL THEN r.value ELSE c.value END AS value,
               c.dims, c.flagged
        FROM cur c
        LEFT JOIN rev r ON r.obs_date = c.obs_date AND r.dims = c.dims
        WHERE c.known_since < (SELECT ts FROM vt)
           -- Vintages before tracking began: points dated by then, at the earliest
           -- value we hold (no pre-tracking revision history unless backfilled).
           OR ((SELECT ts FROM vt) <= %(track)s::timestamptz
               AND c.obs_date < %(vt)s::date AND c.known_since >= %(track)s::timestamptz)
        ORDER BY c.obs_date {order}
        LIMIT %(limit)s
    """
    return db.query(sql, params)


# When revision tracking began (the observation_revisions trigger, 2026-08-17). Points
# dated before this are treated as held since then: vintages earlier than this date
# return the earliest values we hold (documented in /docs/api-vintages).
VINTAGE_TRACKING_START = date(2026, 8, 17)

# Where a superseded value came from: backfills carry a source tag, live revisions
# (our own ingests since VINTAGE_TRACKING_START) have none.
REVISION_SOURCES = {
    None: "PakDataHub revision tracking",
    "sbp_msb": "SBP Monthly Statistical Bulletin",
    "sbp_msb_na": "SBP Monthly Statistical Bulletin (national accounts tables)",
}


def get_revisions(
    series_id: str,
    date_from: date | None = None,
    date_to: date | None = None,
    dims: dict[str, str] | None = None,
    limit: int = 1000,
    sort: str = "desc",
    changed_only: bool = False,
) -> list[dict]:
    """Each point's revision history: today's value, when we first held it
    (`first_seen_at`), and every superseded figure with the date it stopped being
    current, oldest first. The first print is the earliest superseded value when
    there is one, else today's value."""
    import json

    params: dict[str, Any] = {"sid": series_id, "limit": limit}
    clauses = ["o.series_id = %(sid)s"]
    if date_from:
        clauses.append("o.obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("o.obs_date <= %(to)s")
        params["to"] = date_to
    if dims:
        clauses.append("o.dims @> %(dims)s::jsonb")
        params["dims"] = json.dumps(dims)
    if changed_only:
        clauses.append("r.revs IS NOT NULL")
    order = "ASC" if sort == "asc" else "DESC"
    sql = f"""
        SELECT o.obs_date, o.dims, o.value, o.first_seen_at, r.revs
        FROM observations o
        LEFT JOIN LATERAL (
            SELECT json_agg(json_build_object('value', v.value, 'until', v.superseded_at::date,
                                              'source', v.source) ORDER BY v.superseded_at) AS revs
            FROM observation_revisions v
            WHERE v.series_id = o.series_id AND v.obs_date = o.obs_date AND v.dims = o.dims
        ) r ON true
        WHERE {' AND '.join(clauses)}
        ORDER BY o.obs_date {order}
        LIMIT %(limit)s
    """
    out = []
    for row in db.query(sql, params):
        revs = row["revs"] or []
        current = float(row["value"]) if row["value"] is not None else None
        out.append({
            "date": row["obs_date"],
            "dims": row["dims"] or {},
            "value": current,
            "first_print": revs[0]["value"] if revs else current,
            "first_seen": row["first_seen_at"].date() if row["first_seen_at"] else None,
            "revisions": [
                {"value": float(r["value"]) if r["value"] is not None else None, "until": r["until"],
                 "source": REVISION_SOURCES.get(r["source"], r["source"])}
                for r in revs
            ],
        })
    return out


def get_latest(series_id: str) -> list[dict]:
    """Latest observation(s) for a series — one row per distinct dims combo."""
    sql = """
        SELECT DISTINCT ON (dims) obs_date, value, dims, flagged
        FROM observations
        WHERE series_id = %(sid)s
        ORDER BY dims, obs_date DESC
    """
    return db.query(sql, {"sid": series_id})


# Max age (days) before a module is flagged 'delayed' / 'stale', keyed by the
# module's dominant release cadence. Honest, cadence-aware freshness — a daily
# module going 8 days without data is stale; an annual one at 430 days is not.
# One release cycle (+ a few days' grace) per cadence: past this, the latest point is
# the previous release still waiting on the next one (module_status relabels it).
_RELEASE_CYCLE_DAYS = {"monthly": 40, "quarterly": 100, "half_yearly": 190, "annual": 380}

_FRESHNESS_THRESHOLDS = {
    "daily": (3, 8), "weekly": (10, 21),
    # Monthly official data varies in publication lag: FX/monetary land in ~2-3 weeks,
    # but some monthly social/real indicators lag ~2-3 months. Delayed at 45d keeps a
    # broken feed visible; stale only past 95d so a slow-but-current source isn't "behind".
    "monthly": (45, 95),
    # Quarterly macro (external debt, BOP) publishes ~1-2 quarters in arrears — the
    # latest available quarter is routinely ~5-6 months old and NOT "behind".
    "quarterly": (150, 220),
    # Annual official data (esp. fiscal-year public finance) is published with a long
    # lag — the FY ending mid-year is only finalized ~6-18 months later, so the LATEST
    # available annual figure is routinely ~450-550 days old and that is NOT "stale".
    # (400,430) falsely flagged current FY data; (550,640) matches the real cadence.
    "annual": (550, 640), "yearly": (550, 640),
    # Effective-dated prices (PSO fuel): a notice most weekdays now, monthly in the past.
    "irregular": (45, 95),
    # Censuses (population 2023, economic 2023, agriculture 2024): years apart by design.
    "census": (4000, 4500),
    # Household surveys (PSLM, HIES, LFS): rounds 1-5 years apart, published a year or more later.
    "survey": (1500, 2200),
}

# Honest-status: a module/dataset can be OLD for two very different reasons — the
# feeding pipeline is BROKEN (genuinely "stale"), or the pipeline is HEALTHY and the
# SOURCE simply hasn't published the next period yet (quarterly/annual releases lag by
# months). To tell them apart we look at the feeding job's latest run: a recent
# `no_new_data` means "we checked, the source has nothing newer" → label "current"
# (awaiting release), not "stale". A `partial`/`success` that's still behind is a real
# backlog (e.g. EasyData draining) → keep the age-based state. A `failed` feeder → stale.
_MODULE_FEEDERS = {
    "forex": ["easydata_sync"], "monetary": ["easydata_sync"], "social": ["easydata_sync", "pta_telecom"],
    "external": ["easydata_sync"], "debt": ["easydata_sync"], "public-finance": ["easydata_sync"],
    "real": ["easydata_sync", "pbs_lsm"],
    "prices": ["pbs_cpi_monthly", "pbs_cpi_groups", "cost_of_living", "easydata_sync"],
    "commodities": ["pbs_spi_weekly"],
    "fixed-income": ["sbp_kibor", "sbp_policy_rate", "sbp_auctions", "mufap_pkrv"],
    "alternate": ["sbp_payment_systems", "sbp_sme_finance", "pta_telecom", "easydata_sync"],
    "energy": ["pso_fuel_prices", "ismo_power"],
}
_DATASET_FEEDERS = {
    "funds": ["mufap_fund_navs"], "securities": ["mufap_debt_prices"],
    "trade": ["pbs_external_trade", "pbs_trade_hs"], "auctions": ["sbp_auctions"],
    "agri": ["amis_prices", "amis_prices_all", "sindh_amis_prices"],
}
_AWAITING_JOB_MAX_AGE_DAYS = 21  # a no_new_data feeder must have run this recently to count


def _latest_job_runs() -> dict[str, dict]:
    """Latest run per job: {job_name: {status, recent}} where `recent` means the run
    happened within _AWAITING_JOB_MAX_AGE_DAYS (i.e. the pipeline is actively checking)."""
    rows = db.query(
        """
        SELECT DISTINCT ON (job_name) job_name, status,
               (started_at > now() - make_interval(days => %(d)s)) AS recent
        FROM ingestion_runs ORDER BY job_name, started_at DESC
        """,
        {"d": _AWAITING_JOB_MAX_AGE_DAYS},
    )
    return {r["job_name"]: r for r in rows}


def _source_current(feeders: list[str], jobs: dict) -> bool:
    """True if the data is old only because the SOURCE hasn't released more: at least
    one feeder recently returned `no_new_data`, and none is currently `failed`."""
    saw_no_new_data = False
    for j in feeders:
        info = jobs.get(j)
        if not info:
            continue
        if info["status"] == "failed":
            return False  # a broken feeder → genuinely stale, not "awaiting release"
        if info["status"] == "no_new_data" and info.get("recent"):
            saw_no_new_data = True
    return saw_no_new_data


def module_status() -> list[dict]:
    """Per-module freshness for the public /status page: latest observation date,
    dominant cadence, series/observation counts, and an honest fresh/delayed/stale
    state computed against that cadence (not a blanket threshold)."""
    from datetime import date as _date

    sql = """
        SELECT s.module,
               MAX(s.last_date)                         AS last_date,
               COUNT(*)                                 AS series_count,
               COALESCE(SUM(oc.n), 0)::bigint           AS observation_count,
               mode() WITHIN GROUP (ORDER BY lower(s.frequency)) AS dominant_freq,
               array_agg(DISTINCT lower(s.frequency))   AS frequencies
        FROM series s
        LEFT JOIN (
            SELECT series_id, COUNT(*) AS n
            FROM observations GROUP BY series_id
        ) oc ON oc.series_id = s.id
        WHERE s.is_active = true
        GROUP BY s.module
        ORDER BY s.module
    """
    rows = db.query(sql)
    today = _date.today()
    jobs = _latest_job_runs()
    for r in rows:
        present = [f for f in (r.pop("frequencies", None) or []) if f]
        # Judge a module by its DOMINANT (most common) release cadence, not its
        # fastest single series — otherwise a mostly-monthly module (e.g. balance of
        # payments) is wrongly flagged 'stale' at ~34 days just because a handful of
        # daily-tagged series sit alongside the monthly bulk.
        freq = ((r.pop("dominant_freq", None) or (present[0] if present else "")) or "").strip().lower()
        r["frequency"] = freq
        delayed_after, stale_after = _FRESHNESS_THRESHOLDS.get(freq, (45, 70))
        last = r.get("last_date")
        if last is None:
            r["age_days"], r["state"] = None, "stale"
            continue
        # Money-market funds publish weekend/holiday NAVs ahead (Friday carries Mon's),
        # so a max date can sit a few days in the future — never report a negative age.
        if last > today:
            r["last_date"] = last = today
        age = (today - last).days
        r["age_days"] = age
        r["state"] = "stale" if age > stale_after else "delayed" if age > delayed_after else "fresh"
        # "Fresh" only within one release cycle. Older but still inside the lag window
        # (e.g. FY public finance at ~15 months) is the latest the source has published
        # — say "awaiting release", not "current", so an old date never reads as new.
        if r["state"] == "fresh" and age > _RELEASE_CYCLE_DAYS.get(freq, 10**6):
            r["state"] = "current"
            r["awaiting_source"] = True
        # Honest relabel: old-but-source-limited (feeder healthy, recently no_new_data)
        # is "current", not "stale" — the pipeline works, the source just hasn't released.
        if r["state"] in ("stale", "delayed") and _source_current(_MODULE_FEEDERS.get(r["module"], []), jobs):
            r["state"] = "current"
            r["awaiting_source"] = True
    return rows


def list_auctions(
    auction_type: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = 500,
) -> list[dict]:
    """Rows from the dedicated auctions table (Phase 4.4)."""
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if auction_type:
        clauses.append("auction_type = %(t)s")
        params["t"] = auction_type
    if date_from:
        clauses.append("auction_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("auction_date <= %(to)s")
        params["to"] = date_to
    sql = (
        "SELECT auction_type, tenor, auction_date, settlement_date, cutoff_yield, "
        "offered_amount, accepted_amount, bid_to_cover, source "
        f"FROM auctions WHERE {' AND '.join(clauses)} "
        "ORDER BY auction_date DESC, auction_type, tenor LIMIT %(limit)s"
    )
    return db.query(sql, params)


def list_funds(
    amc: str | None = None, category: str | None = None,
    sector: str | None = None, q: str | None = None, limit: int = 1000,
) -> list[dict]:
    """Funds with their latest NAV (Phase: MUFAP fund NAVs)."""
    clauses = ["f.is_active = true"]
    params: dict[str, Any] = {"limit": limit}
    if amc:
        clauses.append("f.amc ILIKE %(amc)s")
        params["amc"] = f"%{amc}%"
    if category:
        clauses.append("f.category ILIKE %(cat)s")
        params["cat"] = f"%{category}%"
    if sector:
        clauses.append("f.sector ILIKE %(sec)s")
        params["sec"] = f"%{sector}%"
    if q:
        clauses.append("f.name ILIKE %(q)s")
        params["q"] = f"%{q}%"
    sql = f"""
        SELECT f.fund_id, f.name, f.amc, f.sector, f.category,
               f.inception_date, f.trustee, f.note, n.obs_date AS nav_date,
               n.nav, n.offer, n.repurchase
        FROM funds f
        LEFT JOIN LATERAL (
            SELECT obs_date, nav, offer, repurchase FROM fund_navs
            WHERE fund_id = f.fund_id ORDER BY obs_date DESC LIMIT 1
        ) n ON true
        WHERE {' AND '.join(clauses)}
        ORDER BY f.name LIMIT %(limit)s
    """
    return db.query(sql, params)


def fund_screener(limit: int = 2000) -> list[dict]:
    """Every active fund with the fields a screener ranks on, in ONE query.

    Joins each fund to its latest NAV, latest trailing-return row (YTD + 1Y=d365
    + 3M=d90 + rating/benchmark) and latest expense row (TER YTD). This is the
    cheap aggregate that backs the ungated Fund Screener tool — it avoids a
    per-fund fan-out (~540×3 calls) to `/returns`, `/expenses` and `/nav`.
    Annualized volatility is a fast-follow (a nightly rollup), left null here.
    """
    sql = """
        SELECT f.fund_id, f.name, f.amc, f.sector, f.category,
               f.inception_date, f.note,
               n.obs_date AS nav_date, n.nav,
               r.obs_date AS returns_date, r.rating, r.benchmark,
               r.ytd, r.d90, r.d365,
               e.obs_date AS expenses_date, e.ter_ytd
        FROM funds f
        LEFT JOIN LATERAL (
            SELECT obs_date, nav FROM fund_navs
            WHERE fund_id = f.fund_id ORDER BY obs_date DESC LIMIT 1
        ) n ON true
        LEFT JOIN LATERAL (
            SELECT obs_date, rating, benchmark, ytd, d90, d365 FROM fund_returns
            WHERE fund_id = f.fund_id ORDER BY obs_date DESC LIMIT 1
        ) r ON true
        LEFT JOIN LATERAL (
            SELECT obs_date, ter_ytd FROM fund_expenses
            WHERE fund_id = f.fund_id ORDER BY obs_date DESC LIMIT 1
        ) e ON true
        WHERE f.is_active = true
        ORDER BY f.name
        LIMIT %(limit)s
    """
    return db.query(sql, {"limit": limit})


def get_fund(fund_id: int) -> dict | None:
    return db.query_one("SELECT * FROM funds WHERE fund_id = %(id)s", {"id": fund_id})


def fund_navs(
    fund_id: int, date_from: date | None = None, date_to: date | None = None,
    limit: int = 1000, sort: str = "desc",
) -> list[dict]:
    clauses = ["fund_id = %(id)s"]
    params: dict[str, Any] = {"id": fund_id, "limit": limit}
    if date_from:
        clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    order = "ASC" if sort == "asc" else "DESC"
    sql = (
        "SELECT obs_date, nav, offer, repurchase FROM fund_navs "
        f"WHERE {' AND '.join(clauses)} ORDER BY obs_date {order} LIMIT %(limit)s"
    )
    return db.query(sql, params)


def fund_returns(
    fund_id: int, date_from: date | None = None, date_to: date | None = None,
    limit: int = 1000, sort: str = "desc",
) -> list[dict]:
    clauses = ["fund_id = %(id)s"]
    params: dict[str, Any] = {"id": fund_id, "limit": limit}
    if date_from:
        clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    order = "ASC" if sort == "asc" else "DESC"
    sql = (
        "SELECT obs_date, rating, benchmark, ytd, mtd, d1, d15, d30, d90, d180, "
        "d270, d365 FROM fund_returns "
        f"WHERE {' AND '.join(clauses)} ORDER BY obs_date {order} LIMIT %(limit)s"
    )
    return db.query(sql, params)


def fund_payouts(fund_id: int, limit: int = 1000) -> list[dict]:
    return db.query(
        "SELECT payout_date, per_unit, ex_nav FROM fund_payouts "
        "WHERE fund_id = %(id)s ORDER BY payout_date DESC LIMIT %(limit)s",
        {"id": fund_id, "limit": limit},
    )


def fund_expenses(fund_id: int, limit: int = 1000) -> list[dict]:
    return db.query(
        "SELECT obs_date, ter_mtd, ter_ytd, mf, sm FROM fund_expenses "
        "WHERE fund_id = %(id)s ORDER BY obs_date DESC LIMIT %(limit)s",
        {"id": fund_id, "limit": limit},
    )


def fund_portfolio(fund_id: int) -> list[dict]:
    """Latest asset-allocation snapshot for a fund (largest weight first)."""
    return db.query(
        """
        SELECT asset_class, percent, as_of_date FROM fund_portfolio
        WHERE fund_id = %(id)s
          AND as_of_date = (SELECT max(as_of_date) FROM fund_portfolio WHERE fund_id = %(id)s)
        ORDER BY percent DESC
        """,
        {"id": fund_id},
    )


def debt_trades(date_from=None, date_to=None, issue: str | None = None,
                limit: int = 500) -> list[dict]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if date_from:
        clauses.append("trade_date >= %(f)s")
        params["f"] = date_from
    if date_to:
        clauses.append("trade_date <= %(t)s")
        params["t"] = date_to
    if issue:
        clauses.append("issue_name ILIKE %(q)s")
        params["q"] = f"%{issue}%"
    return db.query(
        f"""
        SELECT trade_date, bats, issue_name, issue_date, maturity_date, listed,
               face_value, volume, value_mn, price_pct
        FROM debt_trades WHERE {' AND '.join(clauses)}
        ORDER BY trade_date DESC, issue_name LIMIT %(limit)s
        """,
        params,
    )


def list_securities(security_type: str | None = None, q: str | None = None,
                    limit: int = 1000) -> list[dict]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if security_type:
        clauses.append("s.security_type = %(t)s")
        params["t"] = security_type
    if q:
        clauses.append("(s.code ILIKE %(q)s OR s.sbp_code ILIKE %(q)s OR s.name ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    sql = f"""
        SELECT s.code, s.security_type, s.name, s.rating_category, s.sbp_code,
               s.first_seen, s.last_seen,
               p.obs_date AS price_date, p.price, p.net_change
        FROM securities s
        LEFT JOIN LATERAL (
            SELECT obs_date, price, net_change FROM security_prices
            WHERE code = s.code ORDER BY obs_date DESC LIMIT 1
        ) p ON true
        WHERE {' AND '.join(clauses)}
        ORDER BY s.security_type, s.code LIMIT %(limit)s
    """
    return db.query(sql, params)


def security_prices(code: str, date_from: date | None = None,
                    date_to: date | None = None, limit: int = 1000,
                    sort: str = "desc") -> list[dict]:
    clauses = ["code = %(code)s"]
    params: dict[str, Any] = {"code": code, "limit": limit}
    if date_from:
        clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    order = "ASC" if sort == "asc" else "DESC"
    return db.query(
        "SELECT obs_date, price, net_change FROM security_prices "
        f"WHERE {' AND '.join(clauses)} ORDER BY obs_date {order} LIMIT %(limit)s",
        params,
    )


def get_security(code: str) -> dict | None:
    return db.query_one("SELECT * FROM securities WHERE code = %(c)s", {"c": code})


def trade_flows(
    flow: str | None = None, commodity: str | None = None,
    group: str | None = None, date_from: date | None = None,
    date_to: date | None = None, limit: int = 1000, sort: str = "desc",
) -> list[dict]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if flow:
        clauses.append("flow = %(flow)s")
        params["flow"] = flow
    if commodity:
        clauses.append("commodity ILIKE %(c)s")
        params["c"] = f"%{commodity}%"
    if group:
        clauses.append("commodity_group ILIKE %(g)s")
        params["g"] = f"%{group}%"
    if date_from:
        clauses.append("obs_date >= %(from)s")
        params["from"] = date_from
    if date_to:
        clauses.append("obs_date <= %(to)s")
        params["to"] = date_to
    order = "ASC" if sort == "asc" else "DESC"
    return db.query(
        "SELECT flow, commodity, commodity_group, obs_date, unit, quantity, "
        "value_pkr_mn, value_usd_th FROM trade_flows "
        f"WHERE {' AND '.join(clauses)} ORDER BY obs_date {order}, commodity "
        "LIMIT %(limit)s", params)


def catalog_totals() -> dict:
    """The ONE set of headline counts the site, docs and llms.txt use (2026-10-04 —
    the copy had drifted across 16,579 / 22,900 / 23,000+ and 540 / 590 / 600 funds).
    series = active series (what the catalog serves); funds = funds that reported a
    NAV in the last 30 days (excludes closed/matured funds still in the table)."""
    row = db.query_one(
        """
        SELECT (SELECT count(*) FROM series WHERE is_active)::int AS series,
               (SELECT count(DISTINCT fund_id) FROM fund_navs
                 WHERE obs_date > now() - interval '30 days')::int AS funds,
               (SELECT count(DISTINCT amc) FROM funds WHERE is_active)::int AS amcs,
               (SELECT count(*) FROM securities)::int AS securities
        """
    )
    return dict(row or {})


def dataset_status() -> list[dict]:
    """Coverage of the dedicated (non-series) datasets for /v1/status.

    Funds, securities, trade and auctions live in their own tables rather than
    the generic series store, so `module_status()` cannot see them. Without this
    the public trust page silently under-reports the bulk of the product.
    """
    sql = """
        SELECT 'funds'      AS dataset, count(*)::bigint AS records,
               (SELECT count(*) FROM funds)::bigint AS entities,
               min(obs_date) AS first_date, max(obs_date) AS last_date
          FROM fund_navs
        UNION ALL
        SELECT 'securities', count(*)::bigint,
               (SELECT count(*) FROM securities)::bigint,
               min(obs_date), max(obs_date)
          FROM security_prices
        UNION ALL
        SELECT 'trade', count(*)::bigint,
               (SELECT count(DISTINCT commodity) FROM trade_flows)::bigint,
               min(obs_date), max(obs_date)
          FROM trade_flows
        UNION ALL
        SELECT 'auctions', count(*)::bigint,
               (SELECT count(DISTINCT auction_type) FROM auctions)::bigint,
               min(auction_date), max(auction_date)
          FROM auctions
        UNION ALL
        -- AMIS mandi prices: 14M+ rows, so the planner estimate instead of count(*);
        -- min/max ride idx_agri_prices_date.
        SELECT 'agri',
               (SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE relname = 'agri_prices'),
               (SELECT count(*) FROM agri_markets)::bigint,
               (SELECT min(obs_date) FROM agri_prices),
               (SELECT max(obs_date) FROM agri_prices)
        ORDER BY dataset
    """
    rows = db.query(sql)
    # (delayed_after, stale_after, cadence label) per dedicated dataset.
    meta = {
        "funds": (5, 10, "daily"),       # MUFAP NAVs, mon-fri (+ weekend accruals)
        "securities": (6, 12, "daily"),  # debt prices, T+1
        "trade": (45, 70, "monthly"),    # PBS external trade
        "auctions": (25, 55, "periodic"),  # T-bill/PIB/GIS, ~fortnightly
        "agri": (3, 8, "daily"),         # Punjab AMIS mandi prices
    }
    today = date.today()
    jobs = _latest_job_runs()
    for r in rows:
        delayed_after, stale_after, cadence = meta.get(r["dataset"], (30, 60, ""))
        r["frequency"] = cadence
        last = r.get("last_date")
        if last is None:
            r["age_days"], r["state"] = None, "stale"
            continue
        # Money-market funds publish weekend/holiday NAVs ahead (Friday carries Mon's),
        # so a max date can sit a few days in the future — never report a negative age.
        if last > today:
            r["last_date"] = last = today
        age = (today - last).days
        r["age_days"] = age
        r["state"] = "stale" if age > stale_after else "delayed" if age > delayed_after else "fresh"
        if r["state"] in ("stale", "delayed") and _source_current(_DATASET_FEEDERS.get(r["dataset"], []), jobs):
            r["state"] = "current"
            r["awaiting_source"] = True
    return rows


def job_status() -> list[dict]:
    """Per-job last-run status and last-successful-run timestamp, for /status.
    Drives the trust page and doubles as the primary ops dashboard."""
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (job_name)
                   job_name, status, started_at, finished_at, rows_upserted, error
            FROM ingestion_runs
            ORDER BY job_name, started_at DESC
        ),
        last_ok AS (
            SELECT DISTINCT ON (job_name) job_name, started_at AS last_success_at
            FROM ingestion_runs
            WHERE status IN ('success', 'no_new_data', 'partial')
            ORDER BY job_name, started_at DESC
        )
        SELECT l.job_name,
               l.status            AS last_status,
               l.started_at        AS last_run_at,
               l.finished_at       AS last_finished_at,
               l.rows_upserted     AS last_rows,
               ok.last_success_at
        FROM latest l
        LEFT JOIN last_ok ok USING (job_name)
        ORDER BY l.job_name
    """
    return db.query(sql)
