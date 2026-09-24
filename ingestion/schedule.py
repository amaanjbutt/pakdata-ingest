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
    # the VPS/WARP datacenter IP, so they run on GitHub Actions (Azure IPs reach them)
    # writing to this DB over an SSH tunnel. See the pakdata-ingest repo. They remain
    # registered so a manual `python -m ingestion.run <job> --force` still works as a
    # fallback via the residential tunnel.
    # FX now flows through easydata_sync (monthly EasyData exchange-rate series);
    # the daily SBP M2M scrape was retired when SBP removed the source page.
    # Weekly SPI: Friday release + Saturday retry.
    Schedule("pbs_spi_weekly", {"hour": 15, "minute": 0, "day_of_week": "fri"},
             expected_interval=timedelta(days=9)),
    Schedule("pbs_spi_weekly", {"hour": 11, "minute": 0, "day_of_week": "sat"},
             expected_interval=timedelta(days=9)),
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
    # MUFAP jobs are NO LONGER scheduled on the VPS. MUFAP's Cloudflare hard-blocks the
    # VPS WARP egress (the whole 104.28.x WARP range → "you have been blocked"; rotation
    # can't escape it), so they now run on GitHub Actions from Azure runner IPs (see
    # `mufap.yml` in the pakdata-ingest repo — an IP-retry matrix, ~1/5 Azure IPs reach
    # MUFAP). They remain REGISTERED (for a manual `--force` fallback) and are MONITORED
    # for staleness below in MONITORED_EXTERNAL.
    # PBS external trade: monthly release, published mid-month.
    Schedule("pbs_external_trade", {"hour": 14, "minute": 0, "day": "10-20"},
             expected_interval=timedelta(days=40)),
    Schedule("pbs_lsm", {"hour": 15, "minute": 0, "day": "10-25"},
             expected_interval=timedelta(days=40)),
    Schedule("pbs_cpi_monthly", {"hour": 12, "minute": 0, "day": "1-5"},
             expected_interval=timedelta(days=35)),
    Schedule("pbs_cpi_groups", {"hour": 13, "minute": 0, "day": "1-8"},
             expected_interval=timedelta(days=40)),
    Schedule("pta_telecom", {"hour": 10, "minute": 0, "day_of_week": "mon"},
             expected_interval=timedelta(days=40)),
    # Payment Systems Review: quarterly PDF. Check weekly for a new issue;
    # a quarter can lag ~2 months after quarter-end, so allow a long interval.
    Schedule("sbp_payment_systems", {"hour": 9, "minute": 0, "day_of_week": "mon"},
             expected_interval=timedelta(days=130)),
    # SME Finance Review: quarterly PDF, published with a lag; weekly check.
    Schedule("sbp_sme_finance", {"hour": 9, "minute": 30, "day_of_week": "mon"},
             expected_interval=timedelta(days=130)),
]


# Jobs scheduled EXTERNALLY on GitHub Actions (easydata_sync + SBP — see §10),
# NOT run by this scheduler, but still MONITORED here so their staleness surfaces
# and alerts. Without this, the biggest source (EasyData) fails invisibly.
MONITORED_EXTERNAL: list[Schedule] = [
    Schedule("easydata_sync", {}, expected_interval=timedelta(days=3)),
    Schedule("sbp_kibor", {}, expected_interval=timedelta(days=4), stale_on_run=True),
    Schedule("sbp_policy_rate", {}, expected_interval=timedelta(days=14), stale_on_run=True),
    Schedule("sbp_auctions", {}, expected_interval=timedelta(days=14), stale_on_run=True),
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
]


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
