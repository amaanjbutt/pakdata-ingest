"""derive_kibor: EasyData SIRKIBOR series -> flagship rates.kibor.* (pure mapping)."""
from ingestion.jobs.derive_kibor import TENORS, map_source, target_id


def test_map_source_tenors_and_sides():
    assert map_source("banking.three_months_karachi_interbank_offer") == ("3m", "offer")
    assert map_source("banking.one_year_karachi_interbank_bid") == ("1y", "bid")
    assert map_source("banking.two_weeks_karachi_interbank_bid") == ("2w", "bid")
    assert map_source("banking.three_years_karachi_interbank_offer") == ("3y", "offer")


def test_map_source_rejects_other_series():
    assert map_source("banking.three_months_karachi_interbank_mid") is None
    assert map_source("banking.cut_off_yield_3_months") is None
    assert map_source("rates.kibor.3m") is None


def test_every_tenor_has_a_flagship_id():
    assert sorted(target_id(t) for t in TENORS.values()) == sorted(
        f"rates.kibor.{t}" for t in ["1w", "2w", "1m", "3m", "6m", "9m", "1y", "2y", "3y"]
    )
