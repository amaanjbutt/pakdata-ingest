-- PakData API — PostgreSQL schema
-- Core principle: everything is a time series (series_id, date, value, dimensions).

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- search (search_entities, series names)

-- Catalog of every series we publish
CREATE TABLE IF NOT EXISTS series (
    id            TEXT PRIMARY KEY,          -- e.g. 'kibor.3m', 'cpi.national.yoy'
    module        TEXT NOT NULL,             -- 'fixed-income'|'forex'|'commodities'|'economic'|'alternate'
    name          TEXT NOT NULL,
    description   TEXT,
    unit          TEXT,                      -- 'percent','pkr','usd_mn','count','index'
    frequency     TEXT NOT NULL,             -- 'daily','weekly','monthly','quarterly','annual','irregular'
    source        TEXT NOT NULL,             -- 'SBP','PBS','PTA','MUFAP','ECAP','SBP-EasyData'
    source_url    TEXT,
    dimensions    JSONB NOT NULL DEFAULT '{}',
    min_value     NUMERIC,                   -- plausibility gate (nullable = unbounded)
    max_value     NUMERIC,                   -- plausibility gate
    max_step      NUMERIC,                   -- flag if |value - prev| exceeds this (nullable = no check)
    first_date    DATE,
    last_date     DATE,
    tier          TEXT NOT NULL DEFAULT 'basic',  -- 'basic' | 'pro'
    is_active     BOOLEAN NOT NULL DEFAULT true,
    easydata_dataset_code TEXT,                   -- parent EasyData dataset (for cheap freshness checks)
    easydata_last_refresh DATE                    -- source Last Refresh Date last seen
);

-- Backfill columns onto pre-existing installations.
ALTER TABLE series ADD COLUMN IF NOT EXISTS easydata_dataset_code TEXT;
ALTER TABLE series ADD COLUMN IF NOT EXISTS easydata_last_refresh DATE;
-- Derived layer (source 'PakDataHub'): {"method", "inputs", "version"}; NULL = official series.
ALTER TABLE series ADD COLUMN IF NOT EXISTS derivation JSONB;
-- label-duplicate twins point at their canonical series (rel=canonical + noindex on the site)
ALTER TABLE series ADD COLUMN IF NOT EXISTS canonical_id TEXT;

-- Observations
CREATE TABLE IF NOT EXISTS observations (
    series_id   TEXT NOT NULL REFERENCES series(id),
    obs_date    DATE NOT NULL,
    value       NUMERIC,
    dims        JSONB NOT NULL DEFAULT '{}',
    flagged     BOOLEAN NOT NULL DEFAULT false,  -- validation flagged (e.g. large jump)
    revised_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, obs_date, dims)
);
CREATE INDEX IF NOT EXISTS idx_obs_series_date ON observations(series_id, obs_date DESC);

-- Revision history (vintages). Whenever an observation's value changes, a
-- BEFORE-UPDATE trigger records the PRIOR value and the moment it was replaced,
-- so `?vintage=YYYY-MM-DD` can reconstruct the value as we knew it on that date.
-- Append-only; idempotent re-ingests (same value) write nothing thanks to the
-- `IS DISTINCT FROM` gate, so growth tracks genuine revisions only. History
-- accrues from when this ships — it can't be reconstructed retroactively.
CREATE TABLE IF NOT EXISTS observation_revisions (
    series_id      TEXT NOT NULL,
    obs_date       DATE NOT NULL,
    dims           JSONB NOT NULL DEFAULT '{}',
    value          NUMERIC,            -- the prior value that was superseded
    superseded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_obs_rev_lookup
    ON observation_revisions(series_id, obs_date, superseded_at);
-- Provenance of backfilled history: NULL = recorded live by the trigger below;
-- 'sbp_msb' = SBP Monthly Statistical Bulletin archive (scripts/vintages_sbp_msb.py).
ALTER TABLE observation_revisions ADD COLUMN IF NOT EXISTS source TEXT;

-- When we first held each point (2026-10-04). Vintages (?vintage=) need "known since";
-- revised_at can't serve: re-ingesting history used to bump it on every run. NULL =
-- a row from before this column existed (series_repo derives a conservative value).
ALTER TABLE observations ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ;
ALTER TABLE observations ALTER COLUMN first_seen_at SET DEFAULT now();

CREATE OR REPLACE FUNCTION record_observation_revision() RETURNS trigger AS $$
BEGIN
    IF NEW.value IS DISTINCT FROM OLD.value THEN
        INSERT INTO observation_revisions (series_id, obs_date, dims, value, superseded_at)
        VALUES (OLD.series_id, OLD.obs_date, OLD.dims, OLD.value, now());
    ELSE
        -- Unchanged re-ingest: keep the time the current value became current, so
        -- revised_at means "last revised", not "last re-fetched" (fixed 2026-10-04).
        NEW.revised_at := OLD.revised_at;
    END IF;
    -- first_seen_at needs no guard: upserts never name it, so it keeps its value; an
    -- explicit UPDATE of it (vintage backfill / restore) must be allowed through.
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_observation_revision ON observations;
CREATE TRIGGER trg_observation_revision
    BEFORE UPDATE ON observations
    FOR EACH ROW EXECUTE FUNCTION record_observation_revision();

-- Ingestion tracking
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_name      TEXT NOT NULL,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    status        TEXT,                      -- 'success','failed','partial','no_new_data'
    rows_upserted INT NOT NULL DEFAULT 0,
    error         TEXT,
    raw_file      TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_job_time ON ingestion_runs(job_name, started_at DESC);

-- Auctions (Phase 4.4) — the one sanctioned exception to the generic series
-- model: a per-auction record richer than a single time series. Headline cut-off
-- yields are ALSO mirrored into `observations` as auction.<type>.<tenor> series.
-- offered/accepted amounts and bid-to-cover are nullable: they come from SBP DMMD
-- result announcements, to be enriched when a stable machine-readable source is
-- available (the 2026 SBP site redesign dropped the structured result tables).
CREATE TABLE IF NOT EXISTS auctions (
    auction_type    TEXT NOT NULL,             -- 'tbill' | 'pib' | 'gis'
    tenor           TEXT NOT NULL,             -- '3m','6m','12m','3y','10y',...
    auction_date    DATE NOT NULL,
    settlement_date DATE,
    cutoff_yield    NUMERIC,                   -- percent (GIS: cut-off price/rental)
    offered_amount  NUMERIC,                   -- PKR mn (nullable, enrichable)
    accepted_amount NUMERIC,                   -- PKR mn (nullable, enrichable)
    bid_to_cover    NUMERIC,                   -- nullable, enrichable
    source          TEXT NOT NULL DEFAULT 'SBP',
    revised_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (auction_type, tenor, auction_date)
);
CREATE INDEX IF NOT EXISTS idx_auctions_type_date ON auctions(auction_type, auction_date DESC);

-- Mutual funds (MUFAP) — a per-fund entity + daily NAV time series. Modelled as
-- dedicated tables (like auctions) rather than ~1,600 generic series: each fund
-- has rich metadata and the set changes as funds launch/close. `fund_navs` grows
-- one row per fund per day; the daily snapshot accrues history going forward.
CREATE TABLE IF NOT EXISTS funds (
    fund_id        INTEGER PRIMARY KEY,        -- MUFAP FundID
    name           TEXT NOT NULL,
    amc            TEXT,                        -- asset management company
    sector         TEXT,                        -- 'Open-End Funds' | 'Pension' | ...
    category       TEXT,                        -- 'Income', 'Shariah Compliant', ...
    inception_date DATE,
    trustee        TEXT,
    is_active      BOOLEAN NOT NULL DEFAULT true,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A public note on the fund's data (e.g. MUFAP's NAVs for a sub-fund being wound up), set by hand
-- after a source check; shown as `fund.note` on /v1/funds/{id} and on the fund page.
ALTER TABLE funds ADD COLUMN IF NOT EXISTS note TEXT;
CREATE INDEX IF NOT EXISTS idx_funds_amc ON funds(amc);
CREATE INDEX IF NOT EXISTS idx_funds_category ON funds(category);

CREATE TABLE IF NOT EXISTS fund_navs (
    fund_id     INTEGER NOT NULL REFERENCES funds(fund_id),
    obs_date    DATE NOT NULL,                  -- NAV validity date
    nav         NUMERIC,
    offer       NUMERIC,
    repurchase  NUMERIC,
    front_end   NUMERIC,
    back_end    NUMERIC,
    revised_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_navs_date ON fund_navs(obs_date DESC);

-- Daily fund performance/returns snapshot (MUFAP "Performance Summary").
CREATE TABLE IF NOT EXISTS fund_returns (
    fund_id    INTEGER NOT NULL REFERENCES funds(fund_id),
    obs_date   DATE NOT NULL,               -- validity date
    rating     TEXT,
    benchmark  TEXT,
    ytd        NUMERIC, mtd NUMERIC,
    d1 NUMERIC, d15 NUMERIC, d30 NUMERIC, d90 NUMERIC,
    d180 NUMERIC, d270 NUMERIC, d365 NUMERIC,
    revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_returns_date ON fund_returns(obs_date DESC);

-- Fund payout (dividend) events — MUFAP "Payouts".
CREATE TABLE IF NOT EXISTS fund_payouts (
    fund_id     INTEGER NOT NULL REFERENCES funds(fund_id),
    payout_date DATE NOT NULL,
    per_unit    NUMERIC,
    ex_nav      NUMERIC,
    revised_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, payout_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_payouts_date ON fund_payouts(payout_date DESC);

-- Fund expense ratios — MUFAP "Expense Ratios" (TER and its components).
CREATE TABLE IF NOT EXISTS fund_expenses (
    fund_id    INTEGER NOT NULL REFERENCES funds(fund_id),
    obs_date   DATE NOT NULL,               -- validity date
    ter_mtd    NUMERIC,                     -- total expense ratio, month to date %
    ter_ytd    NUMERIC,                     -- total expense ratio, year to date %
    mf         NUMERIC,                     -- management fee %
    sm         NUMERIC,                     -- selling & marketing %
    revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_expenses_date ON fund_expenses(obs_date DESC);

-- Fund asset allocation / portfolio composition (MUFAP, monthly). Long format:
-- one row per (fund, as-of month, asset class) with the percent of the portfolio.
CREATE TABLE IF NOT EXISTS fund_portfolio (
    fund_id     INTEGER NOT NULL REFERENCES funds(fund_id),
    as_of_date  DATE NOT NULL,
    asset_class TEXT NOT NULL,
    percent     NUMERIC,
    revised_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, as_of_date, asset_class)
);
CREATE INDEX IF NOT EXISTS idx_fund_portfolio_fund ON fund_portfolio(fund_id, as_of_date DESC);

-- Trade-level secondary-market transactions in debt securities (MUFAP "Debt
-- Instruments Daily Trading"). One row per reported trade. The natural-key UNIQUE
-- makes re-ingest of overlapping monthly files idempotent.
CREATE TABLE IF NOT EXISTS debt_trades (
    id            BIGSERIAL PRIMARY KEY,
    trade_date    DATE NOT NULL,
    bats          TEXT,               -- 'BATS' | 'Non-BATS'
    issue_name    TEXT NOT NULL,      -- issuer + instrument, e.g. 'K Electric Ltd. Sukuk VI'
    issue_date    DATE,
    maturity_date DATE,
    listed        TEXT,               -- 'Listed' | 'Unlisted'
    face_value    NUMERIC,            -- per certificate
    volume        NUMERIC,            -- number of units
    value_mn      NUMERIC,            -- trade value, Rs in millions
    price_pct     NUMERIC,            -- trade price, % of face value
    revised_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (trade_date, bats, issue_name, maturity_date, volume, value_mn, price_pct)
);
CREATE INDEX IF NOT EXISTS idx_debt_trades_date ON debt_trades(trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_debt_trades_issue ON debt_trades(lower(issue_name));

-- Debt securities (MUFAP daily instrument pricing) — per-instrument prices are a
-- different shape from the tenor curves: thousands of individual bonds/sukuk,
-- each with its own price series. Kept out of the generic series catalog.
CREATE TABLE IF NOT EXISTS securities (
    code          TEXT PRIMARY KEY,          -- e.g. 'PIBFR10YQ2030-10-22', 'GOPIS-27-06-10-2026'
    security_type TEXT NOT NULL,             -- 'pib_floating' | 'gis_sukuk'
    sbp_code      TEXT,                      -- e.g. 'GIS (FRR) -08'
    source        TEXT NOT NULL DEFAULT 'MUFAP',
    first_seen    DATE,
    last_seen     DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_securities_type ON securities(security_type);

CREATE TABLE IF NOT EXISTS security_prices (
    code       TEXT NOT NULL REFERENCES securities(code),
    obs_date   DATE NOT NULL,
    price      NUMERIC,
    net_change NUMERIC,
    revised_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (code, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_security_prices_date ON security_prices(obs_date DESC);

-- Corporate TFC/Sukuk valuations (MUFAP "Debt Instruments Rates") add an issuer
-- name, a rating bucket, and a per-day traded/non-traded marker.
ALTER TABLE securities      ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE securities      ADD COLUMN IF NOT EXISTS rating_category TEXT;
ALTER TABLE security_prices ADD COLUMN IF NOT EXISTS traded_status TEXT;

-- GoP Ijara Sukuk have been published under three different coding schemes over
-- the years (legacy broker matrix, legacy Reuters export, and the current issue
-- -numbered file). All three encode the same two facts, so they are normalised
-- to one canonical code — GOPIS-<VRR|FRR>-<YYYY-MM-DD> — with the rate type and
-- maturity stored as first-class columns rather than buried in a string.
ALTER TABLE securities ADD COLUMN IF NOT EXISTS rate_type TEXT;      -- 'VRR' | 'FRR'
ALTER TABLE securities ADD COLUMN IF NOT EXISTS maturity_date DATE;
CREATE INDEX IF NOT EXISTS idx_securities_maturity ON securities(maturity_date);

-- PBS external trade: monthly export/import by commodity. Hundreds of commodities
-- in a group > item > sub-item hierarchy, each with a quantity and two value
-- measures, so this gets its own table rather than thousands of generic series.
-- Units follow PBS: value_pkr_mn = Rs million, value_usd_th = US$ thousand.
CREATE TABLE IF NOT EXISTS trade_flows (
    flow            TEXT NOT NULL,           -- 'export' | 'import'
    commodity       TEXT NOT NULL,           -- hierarchical label, parent-qualified
    obs_date        DATE NOT NULL,           -- month end of the reporting month
    commodity_group TEXT,                    -- e.g. 'FOOD GROUP'
    unit            TEXT,                    -- e.g. 'M.T'
    quantity        NUMERIC,
    value_pkr_mn    NUMERIC,
    value_usd_th    NUMERIC,
    revised_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (flow, commodity, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_trade_flows_date ON trade_flows(obs_date DESC);
CREATE INDEX IF NOT EXISTS idx_trade_flows_flow ON trade_flows(flow, obs_date DESC);

-- PBS exports/imports by product code x partner country, fiscal years 1990-91 onward
-- (`pbs_trade_hs`). code: 7-digit SITC-based (to FY2001-02) or 8-digit PCT/HS (FY2002-03 ->),
-- 'TOTAL' = all products; country 'All countries' = the code's total line. Value Rs thousand.
CREATE TABLE IF NOT EXISTS trade_hs (
    flow          TEXT NOT NULL,              -- 'export' | 'import' | 're-export' | 're-import'
    fy            SMALLINT NOT NULL,          -- fiscal year ending 30 June (2025 = FY2024-25)
    code          TEXT NOT NULL,
    country       TEXT NOT NULL,
    quantity      DOUBLE PRECISION,
    value_pkr_th  DOUBLE PRECISION,
    PRIMARY KEY (flow, code, fy, country)
);
CREATE INDEX IF NOT EXISTS idx_trade_hs_country ON trade_hs(country, flow, fy);
CREATE INDEX IF NOT EXISTS idx_trade_hs_fy ON trade_hs(flow, fy) WHERE country = 'All countries';
CREATE TABLE IF NOT EXISTS trade_hs_codes (
    code            TEXT PRIMARY KEY,
    classification  TEXT NOT NULL,            -- 'HS' | 'SITC' | 'total'
    description     TEXT NOT NULL,            -- latest PBS wording
    unit            TEXT,
    first_fy        SMALLINT,
    last_fy         SMALLINT
);
-- Readable names (ingestion/trade_names): official HS/SITC text where the code is the official
-- item, else PBS's text expanded; the official parent heading; the unit split back out.
ALTER TABLE trade_hs_codes ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE trade_hs_codes ADD COLUMN IF NOT EXISTS short_name TEXT;
ALTER TABLE trade_hs_codes ADD COLUMN IF NOT EXISTS hs_heading TEXT;
ALTER TABLE trade_hs_codes ADD COLUMN IF NOT EXISTS unit_clean TEXT;

-- Users & billing
CREATE TABLE IF NOT EXISTS users (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email                TEXT UNIQUE NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    plan                 TEXT NOT NULL DEFAULT 'none',   -- 'none','free','developer','pro','business' (legacy 'basic'→developer)
    billing_customer_id  TEXT,
    subscription_status  TEXT                            -- 'active','past_due','canceled'
);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,            -- sha256 of key; plaintext shown once
    user_id     UUID NOT NULL REFERENCES users(id),
    label       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- Dashboard needs a stable, non-secret handle per key (to list/revoke) and a
-- display hint (never the full key). `id` is that handle; `key_prefix` is the
-- first few chars shown as e.g. "pk_live_abcd…".
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_prefix TEXT;
UPDATE api_keys SET id = gen_random_uuid() WHERE id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_id ON api_keys(id);

-- Last time a key authenticated a request (throttled to ~1 write/min/key in
-- app.security.authenticate). Powers the dashboard's "last used" column.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;

-- Email+password auth for the self-serve dashboard. Passwords are pbkdf2-hashed
-- (never stored in plaintext); the dashboard authenticates via a server-side
-- session token (httpOnly cookie), not by pasting an API key.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- Credit-based free tier (2026-08-20): free accounts get a one-time, non-refundable
-- allotment of request credits INSTEAD of a permanent per-day quota. Each API request
-- by a 'free' caller consumes one credit (app.security._consume_credit); at zero the
-- caller gets 402 credits_exhausted and must upgrade. Paid plans are not credit-metered
-- (they use the per-day rate limits in app.security.PLAN_LIMITS). DEFAULT backfills
-- existing free users to the current allotment.
ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_remaining INTEGER NOT NULL DEFAULT 500;
-- Monthly allowance (2026-10-04): the free credits reset at the start of each calendar
-- month. credits_period = the month the current balance belongs to; the first request
-- in a new month refills the balance inside the same atomic UPDATE (no cron).
ALTER TABLE users ADD COLUMN IF NOT EXISTS credits_period DATE NOT NULL
    DEFAULT (date_trunc('month', now())::date);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,           -- sha256 of the session token
    user_id     UUID NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

-- Usage (analytics; live rate limit lives in Redis)
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id   UUID NOT NULL,
    day       DATE NOT NULL,
    requests  INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Billing (LemonSqueezy). Webhook idempotency + one-time API-key hand-off.
ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_id TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS period_end TIMESTAMPTZ;

-- Optional profile fields the user can set from the dashboard Settings tab.
ALTER TABLE users ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS company TEXT;

-- Every processed webhook is recorded so a replay never double-provisions.
CREATE TABLE IF NOT EXISTS billing_events (
    idempotency_key TEXT PRIMARY KEY,   -- the webhook signature (unique per payload)
    event_name      TEXT,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Release calendar: when each indicator is next expected to publish. Rows are
-- generated forward from recurring cadence rules (see db.init._seed_calendar) and
-- can be enriched with the actual value once a release lands.
CREATE TABLE IF NOT EXISTS release_calendar (
    release_date DATE NOT NULL,
    indicator    TEXT NOT NULL,
    source       TEXT NOT NULL,
    series_id    TEXT,                         -- optional link to a catalog series
    actual_value NUMERIC,                      -- filled in once published
    PRIMARY KEY (release_date, indicator)
);
CREATE INDEX IF NOT EXISTS idx_calendar_date ON release_calendar(release_date);

-- Webhooks: a user registers a URL to be POSTed when a series they care about
-- updates. Deliveries are HMAC-signed with the per-hook secret.
CREATE TABLE IF NOT EXISTS webhooks (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id),
    url         TEXT NOT NULL,
    secret      TEXT NOT NULL,                 -- signing secret (shown once at create)
    series_id   TEXT,                          -- NULL = all series in `module`
    module      TEXT,                          -- optional module-level subscription
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_delivery_at TIMESTAMPTZ,
    last_status      INT
);
CREATE INDEX IF NOT EXISTS idx_webhooks_user ON webhooks(user_id);
CREATE INDEX IF NOT EXISTS idx_webhooks_series ON webhooks(series_id) WHERE is_active;

-- One-time key claim: a token (carried through checkout custom_data + the success
-- redirect) maps to the paid user. The plaintext API key is NEVER stored — it is
-- generated only when the token is claimed, returned once, and the token retired.
CREATE TABLE IF NOT EXISTS key_claims (
    token_hash  TEXT PRIMARY KEY,
    user_id     UUID NOT NULL REFERENCES users(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at  TIMESTAMPTZ
);

-- Punjab AMIS daily wholesale (mandi) prices — dedicated tables (10M+ rows of history;
-- kept out of `observations`). ids are AMIS's own market/commodity ids.
CREATE TABLE IF NOT EXISTS agri_markets (
    market_id  INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    is_major   BOOLEAN NOT NULL DEFAULT false   -- fetched daily; the rest weekly
);
CREATE TABLE IF NOT EXISTS agri_commodities (
    commodity_id INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    slug         TEXT NOT NULL,
    category     TEXT,                          -- Grains | Vegetables | Fruits
    unit         TEXT NOT NULL DEFAULT 'pkr_per_100kg'
);
CREATE TABLE IF NOT EXISTS agri_prices (
    commodity_id INTEGER NOT NULL REFERENCES agri_commodities(commodity_id),
    market_id    INTEGER NOT NULL REFERENCES agri_markets(market_id),
    obs_date     DATE NOT NULL,
    min_price    NUMERIC,
    max_price    NUMERIC,
    fqp          NUMERIC,                       -- fair-quality price (the representative price)
    quantity     NUMERIC,                       -- arrivals, when reported
    revised_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (commodity_id, market_id, obs_date)
);
CREATE INDEX IF NOT EXISTS idx_agri_prices_market_date ON agri_prices(market_id, obs_date);
-- Sindh AMIS (2026-10-16) shares these tables: its ids are offset by 900000 (sindh_amis_prices.ID_OFFSET).
ALTER TABLE agri_markets ADD COLUMN IF NOT EXISTS province TEXT NOT NULL DEFAULT 'Punjab';
ALTER TABLE agri_markets ADD COLUMN IF NOT EXISTS district TEXT;
ALTER TABLE agri_commodities ADD COLUMN IF NOT EXISTS province TEXT NOT NULL DEFAULT 'Punjab';
-- readable names + one crop identity across provinces (ingestion/agri_names.apply_names);
-- `province` = the provincial list that reports the market, `region` = where the market is
ALTER TABLE agri_markets ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE agri_markets ADD COLUMN IF NOT EXISTS region TEXT;
ALTER TABLE agri_commodities ADD COLUMN IF NOT EXISTS display_name TEXT;
ALTER TABLE agri_commodities ADD COLUMN IF NOT EXISTS urdu_name TEXT;
ALTER TABLE agri_commodities ADD COLUMN IF NOT EXISTS crop_key TEXT;
CREATE INDEX IF NOT EXISTS idx_agri_prices_date ON agri_prices(obs_date);

-- Data-quality audit (2026-10-14, scripts/dq_audit.py): suspect values found by detectors,
-- whole-series reconciliations against the publisher, and the points that disagreed.
CREATE TABLE IF NOT EXISTS dq_findings (
    id BIGSERIAL PRIMARY KEY, series_id TEXT NOT NULL, obs_date DATE NOT NULL,
    dims JSONB NOT NULL DEFAULT '{}', detector TEXT NOT NULL, tier SMALLINT NOT NULL,
    our_value NUMERIC, context JSONB, verdict TEXT, source_value NUMERIC, evidence TEXT,
    checked_at TIMESTAMPTZ, found_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (series_id, obs_date, dims, detector));
CREATE TABLE IF NOT EXISTS dq_series_checks (
    series_id TEXT NOT NULL, method TEXT NOT NULL, checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_points INT, our_points INT, matched INT, mismatched INT, missing_ours INT,
    missing_source INT, error TEXT, PRIMARY KEY (series_id, method));
CREATE TABLE IF NOT EXISTS dq_mismatches (
    series_id TEXT NOT NULL, obs_date DATE NOT NULL, dims JSONB NOT NULL DEFAULT '{}',
    method TEXT NOT NULL, kind TEXT NOT NULL, our_value NUMERIC, source_value NUMERIC,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (series_id, obs_date, dims, method));

-- Public notes on single points (served as `meta.notes`): a value checked against the
-- publisher and confirmed as published, but implausible (e.g. a likely unit slip at source).
CREATE TABLE IF NOT EXISTS observation_notes (
    series_id  TEXT NOT NULL,
    obs_date   DATE NOT NULL,
    dims       JSONB NOT NULL DEFAULT '{}',
    note       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'as_published',
    evidence   TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (series_id, obs_date, dims)
);

-- Per-request API log (app/services/reqlog → Redis stream → ingestion/request_log.flush).
-- Raw rows live in one partition per day; ingestion/request_log keeps 30 days, exports older
-- days to private storage and drops them. No query strings, no raw IPs (keyed hash only).
CREATE TABLE IF NOT EXISTS api_requests (
    ts       TIMESTAMPTZ NOT NULL,
    user_id  UUID,
    key_id   UUID,
    auth     TEXT,              -- key | session | public
    method   TEXT,
    route    TEXT,              -- route template, e.g. /v1/series/{series_id}
    subject  TEXT,              -- path params / the query param naming what was asked for
    status   SMALLINT,
    ms       REAL,
    bytes    INT,
    ua       TEXT,              -- site | browser | script | curl | sheets | excel | bot | other | none
    ip_hash  TEXT,
    country  TEXT,
    cache    TEXT,              -- hit | miss | '' (endpoint not cached)
    page     TEXT               -- the pakdatahub.com page that made the call (site's server calls)
) PARTITION BY RANGE (ts);
CREATE TABLE IF NOT EXISTS api_requests_default PARTITION OF api_requests DEFAULT;

-- Daily aggregates, kept for good.
CREATE TABLE IF NOT EXISTS usage_daily_endpoint (
    day          DATE NOT NULL,
    user_id      UUID,
    auth         TEXT,
    route        TEXT NOT NULL,
    status_class SMALLINT NOT NULL,   -- 2, 3, 4, 5
    requests     INT NOT NULL,
    ms_p50       REAL,
    ms_p95       REAL,
    bytes        BIGINT,
    cache_hits   INT
);
CREATE INDEX IF NOT EXISTS idx_usage_ep_day ON usage_daily_endpoint (day);
CREATE INDEX IF NOT EXISTS idx_usage_ep_user ON usage_daily_endpoint (user_id, day);
-- Top values per dimension per day: dim in (subject, page, country, ua, route_public).
CREATE TABLE IF NOT EXISTS usage_daily_dim (
    day      DATE NOT NULL,
    dim      TEXT NOT NULL,
    value    TEXT NOT NULL,
    requests INT NOT NULL,
    PRIMARY KEY (day, dim, value)
);

-- Search index for everything that is not a series (ingestion/search_entities.refresh):
-- trade products/countries, districts, plants, mandi crops/markets, funds, AMCs, securities.
CREATE TABLE IF NOT EXISTS search_entities (
    kind      TEXT NOT NULL,
    key       TEXT NOT NULL,
    title     TEXT NOT NULL,
    subtitle  TEXT,
    url       TEXT NOT NULL,
    boost     REAL NOT NULL DEFAULT 0,
    haystack  TEXT NOT NULL,          -- every name it goes by, lower case
    PRIMARY KEY (kind, key)
);
CREATE INDEX IF NOT EXISTS idx_search_entities_trgm ON search_entities USING gin (haystack gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_series_name_trgm ON series USING gin (lower(name) gin_trgm_ops);
