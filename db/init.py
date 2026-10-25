"""Apply schema.sql and seed_catalog.sql to the configured database.

    python -m db.init
"""
from __future__ import annotations

import json
import os

from app import db

HERE = os.path.dirname(__file__)

# EasyData series are catalogued by their dataset code's subject segment
# (TS_GP_<SUBJECT>_<dataset>_<freq>) into product modules, so the 22k+ series are
# discoverable rather than all lumped under 'economic'.
_MODULE_BY_SUBJECT = {
    "ES": "external", "BOP": "external", "FI": "external", "EXT": "external",
    "ED": "debt", "PKDP": "debt", "PDL": "debt",
    "ER": "forex",
    "BAM": "monetary", "MFS": "monetary", "BS": "monetary",
    "FSA": "real", "RLS": "real", "RS": "real", "GA": "real",
    "RL": "social",
    "PT": "prices",
    "PF": "public-finance",
    "IR": "fixed-income",
}


def module_for_dataset(code: str | None, default: str = "economic") -> str:
    """Map an EasyData dataset code (e.g. 'TS_GP_ES_KSORDA_M') to a product module
    via its subject segment. Falls back to the configured module for unknowns."""
    if not code:
        return default
    parts = code.split("_")
    subject = parts[2] if len(parts) > 2 else ""
    return _MODULE_BY_SUBJECT.get(subject, default)


def _run_file(path: str) -> None:
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    print(f"applied {os.path.basename(path)}")


def _seed_easydata_catalog() -> None:
    """Upsert catalog rows for every configured EasyData series."""
    from ingestion.easydata_config import load_series

    rows = load_series()
    with db.connection() as conn:
        with conn.cursor() as cur:
            for s in rows:
                cur.execute(
                    """
                    INSERT INTO series (id, module, name, description, unit, frequency,
                                        source, source_url, dimensions, min_value, max_value,
                                        max_step, tier, easydata_dataset_code,
                                        easydata_last_refresh)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        module=EXCLUDED.module, name=EXCLUDED.name,
                        description=EXCLUDED.description, unit=EXCLUDED.unit,
                        frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                        source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                        max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step,
                        tier=EXCLUDED.tier,
                        easydata_dataset_code=EXCLUDED.easydata_dataset_code,
                        -- keep the later of stored vs configured refresh date
                        easydata_last_refresh=GREATEST(
                            series.easydata_last_refresh,
                            EXCLUDED.easydata_last_refresh)
                    """,
                    (
                        s.id, module_for_dataset(s.easydata_dataset_code, s.module),
                        s.name, s.description, s.unit, s.frequency,
                        s.source,
                        f"https://easydata.sbp.org.pk/api/v1/series/{s.easydata_key}/data",
                        s.min_value, s.max_value, s.max_step, s.tier,
                        s.easydata_dataset_code, s.easydata_last_refresh,
                    ),
                )
    print(f"seeded {len(rows)} EasyData series")


def _seed_pkrv_catalog() -> None:
    """Upsert catalog rows for the MUFAP PKRV (conventional) and PKISRV (Islamic)
    yield-curve tenors."""
    from ingestion.jobs.mufap_pkrv import PKISRV_TENORS, TENORS

    _unit = {"w": "Week", "m": "Month", "y": "Year"}
    curves = [("rates.pkrv", "PKRV", "rupee", TENORS),
              ("rates.pkisrv", "PKISRV", "Islamic (Shariah) rupee", PKISRV_TENORS)]
    total = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for prefix, disp, kind, tenors in curves:
                for t in tenors:
                    num, u = t[:-1], t[-1]
                    label = f"{num}-{_unit[u]}"
                    cur.execute(
                        """
                        INSERT INTO series (id, module, name, description, unit, frequency,
                                            source, source_url, dimensions, min_value, max_value,
                                            max_step, tier)
                        VALUES (%s,'fixed-income',%s,%s,'percent','daily','MUFAP',
                                'https://www.mufap.com.pk/WebRegulations/Index?Head=Pricing&title=PKRV/PKISRV/PKFRV',
                                '{}'::jsonb, 0, 40, 5, 'basic')
                        ON CONFLICT (id) DO UPDATE SET
                            module=EXCLUDED.module, name=EXCLUDED.name,
                            description=EXCLUDED.description, unit=EXCLUDED.unit,
                            frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                            source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                            max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                        """,
                        (f"{prefix}.{t}", f"{disp} {label}",
                         f"MUFAP {disp} {kind} yield curve — {label} tenor"),
                    )
                    total += 1
    print(f"seeded {total} PKRV/PKISRV series")


def _seed_auctions_catalog() -> None:
    """Upsert catalog rows for auction cut-off series (T-Bill/PIB/GIS)."""
    from ingestion.jobs.sbp_auctions import (
        GIS_TENORS, PIB_TENORS, TBILL_TENORS, series_id,
    )

    specs = (
        [("tbill", t, "T-Bill", "percent") for t in TBILL_TENORS]
        + [("pib", t, "PIB", "percent") for t in PIB_TENORS]
        + [("gis", t, "GIS Ijara Sukuk", "index") for t in GIS_TENORS]
    )
    with db.connection() as conn:
        with conn.cursor() as cur:
            for atype, tenor, label, unit in specs:
                sid = series_id(atype, tenor)
                metric = "cut-off price/rental" if atype == "gis" else "cut-off yield"
                cur.execute(
                    """
                    INSERT INTO series (id, module, name, description, unit, frequency,
                                        source, source_url, dimensions, min_value, max_value,
                                        max_step, tier)
                    VALUES (%s,'fixed-income',%s,%s,%s,'irregular','SBP',
                            'https://www.sbp.org.pk/ecodata/kibor/kibor.asp',
                            '{}'::jsonb, %s, %s, %s, 'basic')
                    ON CONFLICT (id) DO UPDATE SET
                        module=EXCLUDED.module, name=EXCLUDED.name,
                        description=EXCLUDED.description, unit=EXCLUDED.unit,
                        frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                        source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                        max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                    """,
                    (sid, f"{label} {tenor.upper()} {metric}",
                     f"{label} auction {metric}, {tenor.upper()} tenor",
                     unit,
                     0, 200 if atype == "gis" else 40, 50 if atype == "gis" else 5),
                )
    print(f"seeded {len(specs)} auction series")


def _seed_cpi_groups_catalog() -> None:
    """Upsert catalog rows for urban CPI by COICOP major group."""
    from ingestion.jobs.pbs_cpi_groups import _GROUPS

    names = {
        "general": "General", "food": "Food & Non-Alcoholic Beverages",
        "alcohol_tobacco": "Alcoholic Beverages & Tobacco",
        "clothing": "Clothing & Footwear",
        "housing": "Housing, Water, Electricity, Gas & Fuels",
        "furnishing": "Furnishing & Household Equipment", "health": "Health",
        "transport": "Transport", "communication": "Communication",
        "recreation": "Recreation & Culture", "education": "Education",
        "restaurants": "Restaurants & Hotels", "misc": "Miscellaneous Goods & Services",
    }
    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for area in ("urban", "rural"):
                for slug in _GROUPS.values():
                    cur.execute(
                        """
                        INSERT INTO series (id, module, name, description, unit, frequency,
                                            source, source_url, dimensions, min_value, max_value,
                                            max_step, tier)
                        VALUES (%s,'prices',%s,%s,'index','monthly','PBS',
                                'https://www.pbs.gov.pk/price-statistics/', '{}'::jsonb, 0, 2000, 60, 'basic')
                        ON CONFLICT (id) DO UPDATE SET
                            module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
                            unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                            source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                            max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                        """,
                        (f"inflation.cpi.{area}.{slug}", f"CPI {area.title()} - {names[slug]}",
                         f"{area.title()} CPI, {names[slug]} group (Base 2015-16)"),
                    )
                    n += 1
    print(f"seeded {n} CPI group series")


def _seed_telecom_catalog() -> None:
    """Upsert catalog rows for every PTA telecom series (single source of truth
    is ingestion.jobs.pta_telecom.INDICATORS)."""
    from ingestion.jobs.pta_telecom import BASE, INDICATORS

    _freq = {"monthly": "monthly", "quarterly": "quarterly", "annual": "annual"}
    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for ind in INDICATORS.values():
                url = BASE.format(id=ind["id"])
                for s in ind["series"]:
                    cur.execute(
                        """
                        INSERT INTO series (id, module, name, description, unit, frequency,
                                            source, source_url, dimensions, min_value, max_value,
                                            max_step, tier)
                        VALUES (%s,'social',%s,%s,%s,%s,'PTA',%s,'{}'::jsonb,%s,%s,NULL,'basic')
                        ON CONFLICT (id) DO UPDATE SET
                            module=EXCLUDED.module, name=EXCLUDED.name,
                            description=EXCLUDED.description, unit=EXCLUDED.unit,
                            frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                            source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                            max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                        """,
                        (s.id, s.name, f"PTA telecom indicator — {s.name}", s.unit,
                         _freq[ind["freq"]], url, s.min_value, s.max_value),
                    )
                    n += 1
    print(f"seeded {n} telecom series")


def _seed_payments_catalog() -> None:
    """Upsert catalog rows for SBP Payment Systems Review series (single source
    of truth is ingestion.jobs.sbp_payment_systems.ALL_SERIES)."""
    from ingestion.jobs.sbp_payment_systems import ALL_SERIES, LANDING

    with db.connection() as conn:
        with conn.cursor() as cur:
            for s in ALL_SERIES:
                cur.execute(
                    """
                    INSERT INTO series (id, module, name, description, unit, frequency,
                                        source, source_url, dimensions, min_value, max_value,
                                        max_step, tier)
                    VALUES (%s,'alternate',%s,%s,%s,'quarterly','SBP',%s,'{}'::jsonb,%s,%s,NULL,'basic')
                    ON CONFLICT (id) DO UPDATE SET
                        module=EXCLUDED.module, name=EXCLUDED.name,
                        description=EXCLUDED.description, unit=EXCLUDED.unit,
                        frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                        source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                        max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                    """,
                    (s.id, s.name, f"SBP Payment Systems Review — {s.name}", s.unit,
                     LANDING, s.min_value, s.max_value),
                )
    print(f"seeded {len(ALL_SERIES)} payment-systems series")


def _seed_sme_catalog() -> None:
    """Upsert catalog rows for SBP SME Finance Review series (single source of
    truth is ingestion.jobs.sbp_sme_finance.ALL_SERIES)."""
    from ingestion.jobs.sbp_sme_finance import ALL_SERIES, LANDING

    with db.connection() as conn:
        with conn.cursor() as cur:
            for s in ALL_SERIES:
                cur.execute(
                    """
                    INSERT INTO series (id, module, name, description, unit, frequency,
                                        source, source_url, dimensions, min_value, max_value,
                                        max_step, tier)
                    VALUES (%s,'alternate',%s,%s,%s,'quarterly','SBP',%s,'{}'::jsonb,%s,%s,NULL,'basic')
                    ON CONFLICT (id) DO UPDATE SET
                        module=EXCLUDED.module, name=EXCLUDED.name,
                        description=EXCLUDED.description, unit=EXCLUDED.unit,
                        frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                        source_url=EXCLUDED.source_url, min_value=EXCLUDED.min_value,
                        max_value=EXCLUDED.max_value, max_step=EXCLUDED.max_step, tier=EXCLUDED.tier
                    """,
                    (s.id, s.name, f"SBP Quarterly SME Finance Review — {s.name}", s.unit,
                     LANDING, s.min_value, s.max_value),
                )
    print(f"seeded {len(ALL_SERIES)} SME finance series")


def _seed_commodities_catalog() -> None:
    """Upsert catalog rows for every configured SPI commodity item."""
    from ingestion.commodities_config import load_cities, load_items

    items = load_items()
    city_keys = sorted(set(load_cities().values()))
    dims = json.dumps({"city": city_keys})
    with db.connection() as conn:
        with conn.cursor() as cur:
            for it in items:
                cur.execute(
                    """
                    INSERT INTO series (id, module, name, description, unit, frequency,
                                        source, source_url, dimensions, min_value, max_value,
                                        max_step, tier)
                    VALUES (%s,'commodities',%s,%s,%s,'weekly','PBS',
                            'https://www.pbs.gov.pk/spi', %s::jsonb, %s,%s,%s,'basic')
                    ON CONFLICT (id) DO UPDATE SET
                        module=EXCLUDED.module, name=EXCLUDED.name,
                        description=EXCLUDED.description, unit=EXCLUDED.unit,
                        frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                        source_url=EXCLUDED.source_url, dimensions=EXCLUDED.dimensions,
                        min_value=EXCLUDED.min_value, max_value=EXCLUDED.max_value,
                        max_step=EXCLUDED.max_step
                    """,
                    (
                        it.id, it.name, f"SPI weekly retail price — {it.name}",
                        it.unit, dims, it.min_value, it.max_value, it.max_step,
                    ),
                )
    print(f"seeded {len(items)} commodity series")


# Jobs that declare their own catalog rows as a module-level CATALOG list of dicts
# (id, module, name, unit, frequency, source, source_url, description, min_value, max_value).
CATALOG_MODULES = [
    "ingestion.jobs.pso_fuel_prices",
    "ingestion.jobs.nepra_generation",
    "ingestion.jobs.pbs_census_2023",
    "ingestion.jobs.pbs_economic_census",
    "ingestion.jobs.pbs_agri_census",
    "ingestion.jobs.pbs_lfs",
    "ingestion.jobs.ismo_power",
    "ingestion.jobs.ismo_merit_order",
    "ingestion.jobs.pbs_census_history",
    "ingestion.jobs.pbs_pslm",
    "ingestion.jobs.pbs_hies",
]


def seed_job_catalogs(modules: list[str] | None = None) -> int:
    """Upsert the catalog rows declared by `CATALOG_MODULES` (or the given modules).
    Safe on prod: touches only those series rows."""
    import importlib

    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for mod in modules or CATALOG_MODULES:
                for s in importlib.import_module(mod).CATALOG:
                    cur.execute(
                        """
                        INSERT INTO series (id, module, name, description, unit, frequency, source,
                                            source_url, dimensions, min_value, max_value, max_step, tier)
                        VALUES (%(id)s, %(module)s, %(name)s, %(description)s, %(unit)s, %(frequency)s,
                                %(source)s, %(source_url)s, %(dimensions)s::jsonb, %(min_value)s,
                                %(max_value)s, NULL, 'basic')
                        ON CONFLICT (id) DO UPDATE SET
                            module=EXCLUDED.module, name=EXCLUDED.name, description=EXCLUDED.description,
                            unit=EXCLUDED.unit, frequency=EXCLUDED.frequency, source=EXCLUDED.source,
                            source_url=EXCLUDED.source_url, dimensions=EXCLUDED.dimensions,
                            min_value=EXCLUDED.min_value, max_value=EXCLUDED.max_value
                        """,
                        {"dimensions": "{}", "min_value": None, "max_value": None, "description": None,
                         "source_url": None, **s,
                         **({"dimensions": json.dumps(s["dimensions"])} if "dimensions" in s else {})},
                    )
                    n += 1
    print(f"seeded {n} job-declared series")
    return n


def _seed_calendar() -> None:
    """Generate upcoming release dates for the headline indicators from simple
    cadence rules (weekly on a weekday, or monthly on ~a day). Regenerated each
    run; ON CONFLICT keeps any already-published rows. Not a full official
    schedule — a forward-looking best estimate that keeps the calendar populated."""
    from datetime import date, timedelta

    today = date.today()
    horizon = today + timedelta(days=120)
    # (indicator, source, series_id, kind, param)
    rules = [
        ("CPI inflation", "PBS", "inflation.cpi.national.yoy", "monthly_day", 1),
        ("Sensitive Price Indicator (weekly)", "PBS", "commodities.wheat_flour", "weekly", 4),
        ("Foreign exchange reserves", "SBP", "reserves.total_sbp_reserves", "weekly", 3),
        ("Workers' remittances", "SBP", "remittances.total", "monthly_day", 10),
        ("Merchandise trade", "PBS", "trade.exports.by_country.total_export_as_bop", "monthly_day", 15),
        ("Large-scale manufacturing", "PBS", "industry.lsm.qim", "monthly_day", 12),
        ("Broad money (M2)", "SBP", "banking.m2", "monthly_day", 20),
    ]
    events: list[tuple] = []
    for indicator, source, sid, kind, param in rules:
        if kind == "weekly":
            d = today
            while d <= horizon:
                if d.weekday() == param:
                    events.append((d, indicator, source, sid))
                d += timedelta(days=1)
        elif kind == "monthly_day":
            y, m = today.year, today.month
            for _ in range(5):
                try:
                    d = date(y, m, param)
                except ValueError:
                    d = date(y, m, 28)
                if today <= d <= horizon:
                    events.append((d, indicator, source, sid))
                m = 1 if m == 12 else m + 1
                y = y + 1 if m == 1 else y

    with db.connection() as conn:
        with conn.cursor() as cur:
            for d, ind, src, sid in events:
                cur.execute(
                    "INSERT INTO release_calendar (release_date, indicator, source, series_id) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (release_date, indicator) DO NOTHING",
                    (d, ind, src, sid),
                )
    print(f"seeded {len(events)} calendar events")


def main() -> None:
    _run_file(os.path.join(HERE, "schema.sql"))
    _run_file(os.path.join(HERE, "seed_catalog.sql"))
    _run_file(os.path.join(HERE, "seed_forex.sql"))
    _run_file(os.path.join(HERE, "seed_policy_rate.sql"))
    _run_file(os.path.join(HERE, "seed_cpi.sql"))
    _run_file(os.path.join(HERE, "seed_lsm.sql"))
    _seed_easydata_catalog()
    _seed_pkrv_catalog()
    _seed_auctions_catalog()
    _seed_cpi_groups_catalog()
    _seed_telecom_catalog()
    _seed_payments_catalog()
    _seed_sme_catalog()
    _seed_commodities_catalog()
    _seed_calendar()
    print("database initialized")


if __name__ == "__main__":
    main()
