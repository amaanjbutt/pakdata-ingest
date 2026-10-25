"""Sindh AMIS rates parser (ingestion/jobs/sindh_amis_prices.py)."""
from datetime import date

from ingestion.jobs.sindh_amis_prices import parse_options, parse_rates

PAGE = """
<select name="market_ids[]" multiple><option value="">All Markets</option><option value="7">Karachi Market</option>
<option value="26">Pano Akil Market</option></select>
<select name="commodity_ids[]"><option value="5">Apple</option><option value="14">Banana, Grade 1 Per Dozen</option>
<option value="192">Apple</option></select>
<table class="table" id="dailyPricesTable"><thead><tr><th>S#</th><th>COMMODITY</th></tr></thead><tbody>
<tr><td>1</td><td> Turai, Grade 2 </td><td>Pano Akil Market</td><td>Sukkur</td><td>16 Oct 2026</td><td><span>PKR 70 / KG</span></td></tr>
<tr><td>2</td><td>Banana, Grade 1 Per Dozen</td><td>Karachi Market</td><td>Karachi (Division)</td><td>15 Oct 2026</td><td>PKR 95 / KG</td></tr>
<tr><td>3</td><td>Apple</td><td>Karachi Market</td><td>Karachi (Division)</td><td>15 Oct 2026</td><td>PKR 0 / KG</td></tr>
</tbody></table>"""


def test_options_and_rates():
    assert parse_options(PAGE, "market_ids[]") == {"karachi market": 7, "pano akil market": 26}
    assert parse_options(PAGE, "commodity_ids[]")["apple"] == 5  # first id wins for a repeated label
    rates = parse_rates(PAGE)
    assert rates[0] == dict(commodity="Turai, Grade 2", market="Pano Akil Market", district="Sukkur",
                            obs_date=date(2026, 10, 16), price=70.0, unit="pkr_per_kg")
    assert rates[1]["unit"] == "pkr_per_dozen"
    assert len(rates) == 2  # a zero rate is not a price


def test_no_table_means_no_rates():
    assert parse_rates("<p>No records found</p>") == []


# The page as served from 2026-10-23 (/market_rates): no DISTRICT column, badges inside cells.
PAGE_2026_10 = """
<table class="table table-striped table-hover align-middle mb-2 p-3" id="dailyPricesTable">
    <thead class="table-success" style="position:sticky;top:0;z-index:5;">
        <tr>
            <th>S#</th>
            <th class="ps-4">COMMODITY</th>
            <th>MARKET NAME</th>
            <th>DATE</th>
            <th>MARKET RATES</th>
        </tr>
    </thead>
    <tbody>
        <tr>
            <td>1</td>
            <td class="ps-4 fw-bold">Orange</td>
            <td><span class="badge bg-primary">Dadu Market</span></td>
            <td><span class="badge bg-secondary">22 Oct 2026</span></td>
            <td>
                <span class="badge bg-success fs-6">
                    PKR 100 / KG
                </span>
            </td>
        </tr>
        <tr>
            <td>2</td>
            <td class="ps-4 fw-bold">Banana, Grade 1 Per Dozen</td>
            <td><span class="badge bg-primary">Karachi Market</span></td>
            <td><span class="badge bg-secondary">21 Oct 2026</span></td>
            <td><span class="badge bg-success fs-6">PKR 1,250 / KG</span></td>
        </tr>
    </tbody>
</table>"""


def test_rates_without_district_column():
    rates = parse_rates(PAGE_2026_10)
    assert rates == [
        dict(commodity="Orange", market="Dadu Market", district=None, obs_date=date(2026, 10, 22),
             price=100.0, unit="pkr_per_kg"),
        dict(commodity="Banana, Grade 1 Per Dozen", market="Karachi Market", district=None,
             obs_date=date(2026, 10, 21), price=1250.0, unit="pkr_per_dozen"),
    ]
