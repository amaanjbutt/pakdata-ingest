"""Query layer over the series catalog and observations store.

Keeps SQL in one place; everything is parameterized.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app import db


def list_series(
    module: str | None = None, source: str | None = None, q: str | None = None,
    limit: int | None = None,
) -> list[dict]:
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
    sql = f"SELECT * FROM series WHERE {' AND '.join(clauses)} ORDER BY id"
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    return db.query(sql, params)


def count_series(module: str | None = None, source: str | None = None,
                 q: str | None = None) -> int:
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
    row = db.query_one(f"SELECT count(*) AS c FROM series WHERE {' AND '.join(clauses)}", params)
    return int(row["c"]) if row else 0


def get_series(series_id: str) -> dict | None:
    return db.query_one("SELECT * FROM series WHERE id = %(id)s", {"id": series_id})


def search_series(q: str, module: str | None = None, limit: int = 20) -> list[dict]:
    """Ranked catalog search for the ⌘K palette. Orders exact-id, then id-prefix,
    then name, then description matches, so the most relevant series come first."""
    params: dict[str, Any] = {
        "q": q,
        "like": f"%{q}%",
        "prefix": f"{q}%",
        "limit": limit,
    }
    clauses = ["is_active = true", "(id ILIKE %(like)s OR name ILIKE %(like)s OR description ILIKE %(like)s)"]
    if module:
        clauses.append("module = %(module)s")
        params["module"] = module
    sql = f"""
        SELECT id, module, name, unit, frequency, source, tier
        FROM series
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE
                WHEN lower(id) = lower(%(q)s)   THEN 0
                WHEN id ILIKE %(prefix)s        THEN 1
                WHEN name ILIKE %(prefix)s      THEN 2
                WHEN name ILIKE %(like)s        THEN 3
                ELSE 4
            END,
            length(id),
            name
        LIMIT %(limit)s
    """
    return db.query(sql, params)


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


def get_latest(series_id: str) -> list[dict]:
    """Latest observation(s) for a series — one row per distinct dims combo."""
    sql = """
        SELECT DISTINCT ON (dims) obs_date, value, dims, flagged
        FROM observations
        WHERE series_id = %(sid)s
        ORDER BY dims, obs_date DESC
    """
    return db.query(sql, {"sid": series_id})


def module_status() -> list[dict]:
    """Per-module freshness for the public /status page: latest observation date,
    series count, and total observation count."""
    sql = """
        SELECT s.module,
               MAX(s.last_date)                         AS last_date,
               COUNT(*)                                 AS series_count,
               COALESCE(SUM(oc.n), 0)::bigint           AS observation_count
        FROM series s
        LEFT JOIN (
            SELECT series_id, COUNT(*) AS n
            FROM observations GROUP BY series_id
        ) oc ON oc.series_id = s.id
        WHERE s.is_active = true
        GROUP BY s.module
        ORDER BY s.module
    """
    return db.query(sql)


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
               f.inception_date, f.trustee, n.obs_date AS nav_date,
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
        ORDER BY dataset
    """
    return db.query(sql)


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
