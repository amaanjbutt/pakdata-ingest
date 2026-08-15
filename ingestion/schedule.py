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
    # Nightly incremental EasyData sync (cheap dataset/meta freshness check).
    Schedule("easydata_sync", {"hour": 20, "minute": 0}, expected_interval=2 * DAY),
    # KIBOR — page updates midday; check thrice on weekdays.
    Schedule("sbp_kibor", {"hour": "13,17,19", "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    # FX now flows through easydata_sync (monthly EasyData exchange-rate series);
    # the daily SBP M2M scrape was retired when SBP removed the source page.
    # Weekly SPI: Friday release + Saturday retry.
    Schedule("pbs_spi_weekly", {"hour": 15, "minute": 0, "day_of_week": "fri"},
             expected_interval=timedelta(days=9)),
    Schedule("pbs_spi_weekly", {"hour": 11, "minute": 0, "day_of_week": "sat"},
             expected_interval=timedelta(days=9)),
    # ---- declared ahead of implementation (skipped until registered) ----------
    Schedule("sbp_policy_rate", {"hour": 18, "minute": 0},
             expected_interval=timedelta(days=60), stale_on_run=True),
    Schedule("mufap_pkrv", {"hour": 18, "minute": 0, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    # Daily NAV snapshot for every mutual fund (published each evening).
    Schedule("mufap_fund_navs", {"hour": 19, "minute": 0, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    Schedule("mufap_fund_returns", {"hour": 19, "minute": 30, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    Schedule("mufap_fund_stats", {"hour": 20, "minute": 30, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    # Portfolio/asset-allocation is a monthly snapshot (~540 per-fund calls) — run
    # weekly to catch month-end updates without hammering the source.
    Schedule("mufap_fund_portfolio", {"day_of_week": "sat", "hour": 6, "minute": 0},
             expected_interval=timedelta(days=10)),
    Schedule("mufap_debt_prices", {"hour": 18, "minute": 30, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    Schedule("mufap_debt_trades", {"hour": 21, "minute": 0, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    Schedule("mufap_tfc_valuations", {"hour": 18, "minute": 45, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=4)),
    Schedule("sbp_auctions", {"hour": 16, "minute": 0, "day_of_week": "mon-fri"},
             expected_interval=timedelta(days=9)),
    Schedule("ecap_open_market", {"hour": "12,18"},
             expected_interval=timedelta(days=2)),
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


def expected_intervals() -> dict[str, timedelta]:
    """Tightest expected_interval per job (used by the staleness checker)."""
    out: dict[str, timedelta] = {}
    for s in SCHEDULES:
        cur = out.get(s.job)
        if cur is None or s.expected_interval < cur:
            out[s.job] = s.expected_interval
    return out


def stale_on_run_jobs() -> set[str]:
    return {s.job for s in SCHEDULES if s.stale_on_run}
