"""PSO POL price archive parser (ingestion/jobs/pso_fuel_prices.py)."""
from datetime import date

from ingestion.jobs.pso_fuel_prices import clean_notices, last_page, parse_page


def _notice(d: str, petrol: str, diesel: str = "Rs.80/Ltr", sko: str = "--") -> str:
    return (f'<a class="uk-accordion-title">Effective From: {d} <img/></a><div><table>'
            "<tr><th>Product Name</th><th>Rs./Litre</th></tr>"
            f"<tr><td>E10 GASOLINE</td><td>Rs.0/Ltr</td></tr><tr><td>PREMIER EURO 5</td><td>{petrol}</td></tr>"
            f"<tr><td>HI-CETANE DIESEL EURO 5</td><td>{diesel}</td></tr><tr><td>SKO</td><td>{sko}</td></tr>"
            f"<tr><td>SKO</td><td>{sko}</td></tr></table></div>")


def test_parse_page_reads_every_notice_and_skips_unsold_products():
    page = _notice("October 16, 2026", "Rs.390.23/Ltr", sko="Rs.330.41/Ltr") + _notice("October 15, 2026", "Rs.393.86/Ltr")
    page += '<a href="?page=2">2</a><a href="?page=47">47</a>'
    notices = parse_page(page)
    assert notices[0] == (date(2026, 10, 16), {"PREMIER EURO 5": 390.23, "HI-CETANE DIESEL EURO 5": 80.0, "SKO": 330.41})
    assert "E10 GASOLINE" not in notices[1][1] and "SKO" not in notices[1][1]
    assert last_page(page) == 47


def test_clean_drops_misdated_notices_spikes_and_duplicates():
    notices = parse_page(
        _notice("June 01, 2016", "Rs.64.27/Ltr") + _notice("May 02, 2016", "Rs.2222/Ltr")
        + _notice("May 01, 2016", "Rs.64.27/Ltr")
        + _notice("January 01, 2006", "Rs.62.5/Ltr")          # placeholder date between 2016 notices
        + _notice("April 01, 2016", "Rs.62.5/Ltr")
        + _notice("March 16, 2016", "Rs.62.0/Ltr") + _notice("March 16, 2016", "Rs.61.0/Ltr"))
    records, dropped = clean_notices(notices)
    petrol = {(r.obs_date, r.value) for r in records if r.series_id == "energy.fuel.petrol"}
    assert (date(2016, 5, 2), 2222.0) not in petrol
    assert all(d != date(2006, 1, 1) for d, _ in petrol)
    assert (date(2016, 3, 16), 62.0) in petrol and (date(2016, 3, 16), 61.0) not in petrol
    assert any("misdated" in d for d in dropped) and any("spike" in d for d in dropped)
