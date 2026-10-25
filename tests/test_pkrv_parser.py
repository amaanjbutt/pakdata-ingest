from __future__ import annotations

import os
from datetime import date

import pytest

from ingestion.jobs.mufap_pkrv import parse_pkisrv_csv, parse_pkrv_csv

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
JUL = os.path.join(FIX, "pkrv_2026-07-22.csv")
DEC = os.path.join(FIX, "pkrv_2024-12-31.csv")
PKISRV = os.path.join(FIX, "pkisrv_2026-07-23.csv")


def _load(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("path,d", [(JUL, date(2026, 7, 22)), (DEC, date(2024, 12, 31))])
def test_parses_full_curve(path, d):
    recs = parse_pkrv_csv(_load(path), d)
    ids = {r.series_id for r in recs}
    # short end through long end present
    assert {"pkrv.1w", "pkrv.3m", "pkrv.1y", "pkrv.10y", "pkrv.20y"} <= ids
    assert len(recs) >= 18
    assert all(r.obs_date == d for r in recs)
    assert all(0 < r.value < 40 for r in recs)


def test_known_values_july():
    recs = parse_pkrv_csv(_load(JUL), date(2026, 7, 22))
    by = {r.series_id: r.value for r in recs}
    assert by["pkrv.1w"] == 11.52
    assert by["pkrv.10y"] == 12.09
    assert by["pkrv.20y"] == 12.39


def test_curve_is_dimensionless():
    recs = parse_pkrv_csv(_load(JUL), date(2026, 7, 22))
    assert all(r.dims == {} for r in recs)


def test_rejects_non_pkrv_csv():
    with pytest.raises(ValueError):
        parse_pkrv_csv("foo,bar\n1,2\n", date(2026, 1, 1))


# ---- PKISRV (Islamic curve, embedded in right-hand columns) ------------------

def test_parses_pkisrv_embedded_curve():
    recs = parse_pkisrv_csv(_load(PKISRV), date(2026, 7, 23))
    by = {r.series_id: r.value for r in recs}
    # 5-tenor Islamic benchmark curve
    assert set(by) == {"pkisrv.1m", "pkisrv.3m", "pkisrv.6m", "pkisrv.9m", "pkisrv.1y"}
    assert by["pkisrv.1m"] == 11.47
    assert by["pkisrv.6m"] == 10.35
    assert all(r.dims == {} and 0 < r.value < 40 for r in recs)


def test_pkisrv_ignores_instrument_prices():
    # The left-hand GOPIS sukuk price columns must not leak into the curve.
    recs = parse_pkisrv_csv(_load(PKISRV), date(2026, 7, 23))
    assert all(r.series_id.startswith("pkisrv.") for r in recs)


def test_pkisrv_missing_curve_raises():
    with pytest.raises(ValueError):
        parse_pkisrv_csv("FMA Code,Price\nX,100\n", date(2026, 1, 1))


# ---- older layout: a panel of broker quotes, the PKRV is the FMAP column ----------------

BROKER = os.path.join(FIX, "pkrv_2024-02-02_broker_panel.csv")


def test_broker_panel_reads_fmap_not_first_broker():
    # MUFAP's 02-02-2024 file: AHL quoted 0 for every tenor; FMAP (the published average)
    # is the rate. Reading column 1 stored 0.00% for 2y-20y.
    recs = {r.series_id: r.value for r in parse_pkrv_csv(_load(BROKER), date(2024, 2, 2))}
    assert recs["pkrv.2y"] == 17.58
    assert recs["pkrv.10y"] == 14.18
    assert all(v > 0 for v in recs.values())


def test_broker_panel_day_buckets_map_to_tenors():
    recs = {r.series_id: r.value for r in parse_pkrv_csv(_load(BROKER), date(2024, 2, 2))}
    assert recs["pkrv.1w"] == 21.7    # 0-7days
    assert recs["pkrv.3m"] == 20.79   # 61-90days
    assert recs["pkrv.1y"] == 20.96   # 271-365days
    assert len(recs) == 20


def test_non_positive_rates_are_skipped():
    recs = parse_pkrv_csv("Tenor,Mid Rate,Change\n1W,0.00,0\n1Y,12.1,0\n", date(2026, 1, 1))
    assert [r.series_id for r in recs] == ["pkrv.1y"]
