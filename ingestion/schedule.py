"""Declarative schedule for ingestion jobs (Phase 1).

Schedules are declared here in code — no Airflow, no external config. The
scheduler service (`ingestion.scheduler`) reads this table, wires each job whose
name is present in the `JOBS` registry into APScheduler, and runs a periodic
staleness check driven by each job's `expected_interval`.

Cron fields are interpreted in **Asia/Karachi** (see `app.config.settings.timezone`).
Jobs listed here but not yet in the registry are logged and skipped, so future
sources can be declared ahead of their implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Schedule:
    job: str
    # APScheduler cron fields (Asia/Karachi). Any subset; unspecified => "*".
    cron: dict
    # How long after the last *successful* run before the job is considered stale.
    # Sized generously above the release cadence to avoid false alarms.
    expected_interval: timedelta
    # Some jobs legitimately return no rows most runs (e.g. policy rate changes
    # only on MPC days). For those, staleness is measured from last *run*, not
    # last successful data change.
    stale_on_run: bool = False


DAY = timedelta(days=1)

SCHEDULES: list[Schedule] = [
    # NOTE: easydata_sync + the SBP jobs (sbp_kibor / sbp_policy_rate / sbp_auctions)
    # are NOT scheduled here — their sources (easydata.sbp.org.pk, sbp.org.pk) block
    # the VPS datacenter IP, so they run on GitHub Actions (Azure IPs reach them)
    # writing to this DB over an SSH tunnel (ops/gha-ingest/workflows/ingest.yml).
    # They stay registered so `python -m ingestion.run <job> --force` works anywhere.
    # Daily interbank FX (M2M + bid/offer) is parsed by sbp_kibor from kibor.asp;
    # monthly FX history flows through easydata_sync.
    # Weekly SPI now runs on GitHub Actions (pbs.yml, Fri + Sat) — see MONITORED_EXTERNAL.
    # Derived per-city cost-of-living index — recompute shortly after each SPI
    # ingest (reads the DB, no external fetch).
    Schedule("cost_of_living", {"hour": 15, "minute": 30, "day_of_week": "fri"},
             expected_interval=timedelta(days=9)),
    Schedule("cost_of_living", {"hour": 11, "minute": 30, "day_of_week": "sat"},
             expected_interval=timedelta(days=9)),
    # Derived auction cut-offs (T-Bill/PIB) from the EasyData SIRTBIL/SIRPIBS series —
    # reliable + deep history vs the stuck kibor.asp scrape. Pure DB derivation; run
    # daily after the EasyData syncs land (GHA 01/13 + 07/19 UTC).
    Schedule("derive_auctions", {"hour": 9, "minute": 0},
             expected_interval=timedelta(days=14)),
    # Full KIBOR history (2005→) into the flagship rates.kibor.* ids from the EasyData
    # SIRKIBOR series. Pure DB derivation; the same-day sbp_kibor scrape wins on overlap.
    Schedule("derive_kibor", {"hour": 9, "minute": 15},
             expected_interval=timedelta(days=14)),
    # The PakDataHub derived layer (real rates, spreads, import cover, essentials
    # inflation, fund-category yields...) — recomputed after its inputs land: the
    # morning EasyData/KIBOR derivations, the evening MUFAP runs, and Saturday's SPI.
    # Punjab AMIS wholesale prices (reachable from the VPS): the ~22 major mandis every
    # evening once the day's prices are in; every market weekly in the quiet early hours.
    Schedule("amis_prices", {"hour": 21, "minute": 30},
             expected_interval=timedelta(days=3)),
    Schedule("amis_prices_all", {"hour": 3, "minute": 30, "day_of_week": "sun"},
             expected_interval=timedelta(days=9)),
    # Sindh AMIS fruit & vegetable rates (reachable from the VPS), entered over the day.
    Schedule("sindh_amis_prices", {"hour": 21, "minute": 45},
             expected_interval=timedelta(days=3)),
    # PSO retail fuel prices (psopk.com answers the VPS): PSO now posts a notice most
    # weekdays; check each morning and evening. A quiet stretch is normal, hence stale_on_run.
    Schedule("pso_fuel_prices", {"hour": 9, "minute": 5},
             expected_interval=timedelta(days=3), stale_on_run=True),
    Schedule("pso_fuel_prices", {"hour": 19, "minute": 5},
             expected_interval=timedelta(days=3), stale_on_run=True),
    Schedule("derive_indicators", {"hour": 9, "minute": 45},
             expected_interval=timedelta(days=3)),
    Schedule("derive_indicators", {"hour": 23, "minute": 30},
             expected_interval=timedelta(days=3)),
    # Nightly data-quality sweep (quarantine NAV spikes / impossible prices & yields,
    # correct unambiguous SBP unit slips) after the MUFAP (19/22 PKT) + EasyData runs.
    Schedule("data_quality", {"hour": 5, "minute": 30},
             expected_interval=timedelta(days=3)),
    Schedule("data_quality", {"hour": 12, "minute": 30},
             expected_interval=timedelta(days=3)),
    # MUFAP jobs are NO LONGER scheduled on the VPS. MUFAP's Cloudflare hard-blocks the
    # VPS WARP egress (the whole 104.28.x WARP range → "you have been blocked"; rotation
    # can't escape it), so they now run on GitHub Actions from Azure runner IPs (see
    # `mufap.yml` in the pakdata-ingest repo — an IP-retry matrix, ~1/5 Azure IPs reach
    # MUFAP). They remain REGISTERED (for a manual `--force` fallback) and are MONITORED
    # for staleness below in MONITORED_EXTERNAL.
    # PBS (SPI, CPI, CPI groups, LSM, trade): www.pbs.gov.pk has dropped connections
    # from this VPS since ~2026-10-05, so these run on GitHub Actions (`pbs.yml` in
    # pakdata-ingest, on the days each release can land) and are monitored below.
    # Rebuild the bulletin series + revision vintages when sbp_msb_fetch (GHA) stored a
    # new SBP Monthly Statistical Bulletin issue; a cheap no-op otherwise.
    Schedule("vintages_refresh", {"hour": 6, "minute": 15},
             expected_interval=timedelta(days=3)),
    # SBP's Payment Systems / SME Finance reviews: sbp.org.pk 403s this VPS (since ~Aug-2026),
    # so they run on GitHub Actions (`publications.yml`) and are monitored below.
    # PTA telecom indicators: pta.gov.pk drops connections from this VPS and from GitHub's
    # runners but answers Cloudflare's network, so the job fetches through the relay Worker
    # (ops/relay/; FETCH_RELAY_URL + FETCH_RELAY_KEY in .env). Monthly source; a daily run is
    # ten small pages and reads `no_new_data` until PTA publishes the next month.
    Schedule("pta_telecom", {"hour": 11, "minute": 20}, expected_interval=timedelta(days=40)),
]


# Jobs scheduled EXTERNALLY on GitHub Actions (easydata_sync + SBP — see §10),
# NOT run by this scheduler, but still MONITORED here so their staleness surfaces
# and alerts. Without this, the biggest source (EasyData) fails invisibly.
MONITORED_EXTERNAL: list[Schedule] = [
    Schedule("easydata_sync", {}, expected_interval=timedelta(days=3)),
    Schedule("sbp_kibor", {}, expected_interval=timedelta(days=4), stale_on_run=True),
    Schedule("sbp_policy_rate", {}, expected_interval=timedelta(days=14), stale_on_run=True),
    Schedule("sbp_auctions", {}, expected_interval=timedelta(days=14), stale_on_run=True),
    # PBS on GitHub Actions (pbs.yml) — same intervals as the old VPS cadence.
    Schedule("pbs_spi_weekly", {}, expected_interval=timedelta(days=9)),
    Schedule("pbs_cpi_monthly", {}, expected_interval=timedelta(days=35)),
    Schedule("pbs_cpi_groups", {}, expected_interval=timedelta(days=40)),
    Schedule("pbs_lsm", {}, expected_interval=timedelta(days=40)),
    Schedule("pbs_external_trade", {}, expected_interval=timedelta(days=40)),
    # publications.yml (Mon + Thu): SBP reviews, quarterly with a lag.
    Schedule("sbp_payment_systems", {}, expected_interval=timedelta(days=130)),
    Schedule("sbp_sme_finance", {}, expected_interval=timedelta(days=130)),
    # SBP Monthly Statistical Bulletin (publications.yml); a new issue roughly monthly.
    Schedule("sbp_msb_fetch", {}, expected_interval=timedelta(days=10)),
    # MUFAP now runs on GitHub Actions (Azure IPs) — see mufap.yml in pakdata-ingest.
    # Not scheduled here; monitored so a persistent GHA/MUFAP outage still surfaces via
    # the hourly staleness_check. Intervals match the old VPS cadence.
    # MUFAP runs on the GHA IP-retry matrix — only ~1/5 Azure IPs reach MUFAP, so
    # ingestion lands probabilistically and a normal unlucky stretch can span several
    # days (weekday-only cron + IP luck). 7 days tolerates that variance so staleness
    # only fires on a genuinely concerning gap, not routine bad luck. (Was 4 → false
    # staleness alerts on self-healing gaps.)
    Schedule("mufap_fund_navs", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_fund_returns", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_fund_stats", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_pkrv", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_debt_prices", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_debt_trades", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_tfc_valuations", {}, expected_interval=timedelta(days=7)),
    Schedule("mufap_fund_portfolio", {}, expected_interval=timedelta(days=10)),
    # ISMO (energy.yml, daily): ismo.gov.pk answers GitHub's runners (and lately the VPS); the
    # merit orders are scanned tables that need OCR, which the api image doesn't ship. Monthly
    # market workbooks; a new merit order every one to two weeks.
    Schedule("ismo_power", {}, expected_interval=timedelta(days=45)),
    Schedule("ismo_merit_order", {}, expected_interval=timedelta(days=30)),
]


# Registered jobs run by hand from a workstation (the publishers block the VPS and/or
# GitHub's runners, or the source is annual/one-off): fetch + parse on the PC, then load
# the records inside the api container (docs/ingestion.md). Not scheduled, not monitored.
WORKSTATION: frozenset[str] = frozenset({
    "nepra_generation", "pbs_census_2023", "pbs_census_history", "pbs_economic_census",
    "pbs_agri_census", "pbs_lfs", "pbs_hies", "pbs_pslm", "pbs_trade_hs",
})


def expected_intervals() -> dict[str, timedelta]:
    """Tightest expected_interval per job (used by the staleness checker). Covers
    both VPS-scheduled jobs and the externally-scheduled GHA jobs we monitor."""
    out: dict[str, timedelta] = {}
    for s in [*SCHEDULES, *MONITORED_EXTERNAL]:
        cur = out.get(s.job)
        if cur is None or s.expected_interval < cur:
            out[s.job] = s.expected_interval
    return out


def stale_on_run_jobs() -> set[str]:
    return {s.job for s in [*SCHEDULES, *MONITORED_EXTERNAL] if s.stale_on_run}
