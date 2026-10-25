"""Pure parsers added for the 2026-10-17 granular-data batch: trade by product x country, ISMO
power market and merit order, census history, PSLM districts, and the shared geography map."""
from datetime import date

from ingestion import geo_pk
from ingestion.jobs import ismo_merit_order as mo
from ingestion.jobs import ismo_power as ip
from ingestion.jobs import pbs_census_history as ch
from ingestion.jobs import pbs_pslm as pslm
from ingestion.jobs import pbs_trade_hs as hs


# ---- geography -------------------------------------------------------------------------------------

def test_canon_district_aliases_and_fuzzy():
    assert geo_pk.canon_district("Bahawalnager") == ("Punjab", "Bahawalnagar")
    assert geo_pk.canon_district("D.G.khan") == ("Punjab", "Dera Ghazi Khan")
    assert geo_pk.canon_district("Jaccobabad") == ("Sindh", "Jacobabad")
    assert geo_pk.canon_district("Muzaffar Garh", "Punjab") == ("Punjab", "Muzaffargarh")
    assert geo_pk.canon_district("Karachi") == ("Sindh", "Karachi")  # pre-split district stays itself
    assert geo_pk.canon_district("Lahore", "Sindh") is None or geo_pk.canon_district("Lahore", "Sindh")[0] == "Punjab"
    assert geo_pk.canon_district("Total") is None
    assert geo_pk.canon_province("KPK") == "Khyber Pakhtunkhwa"


# ---- trade by product x country ------------------------------------------------------------------

_TXT = """\
                                       EXPORTS BY COMMODITIES AND COUNTRIES  PAKISTAN
                                                            2 0 1 5 - 2 0 1 6                               2 0 1 4 - 2 0 1 5
 HSCODE COMMODITY / COUNTRY            UNIT         JUN            CUMULATIVE FROM JULY          JUN            CUMULATIVE FROM JULY
                                              QUANTITY     VALUE    QUANTITY      VALUE    QUANTITY     VALUE    QUANTITY      VALUE
         G R A N D  T O T A L                          172176338             2166846412             184069171             2397512960

01051100 FOWLS (CHICKEN) WT UPTO 185G  NO        1800        188      22940        2454         --         --       4715        1985
               Afghanistan                       1800        188      22900        2422         --         --         --          --
               Oman                                --         --         40          32         --         --       1815         752
                                                                                                                        PAGE   96
"""


def test_trade_text_layout_reconciles():
    rows, meta = hs.parse_file("EXPORTS_2015-2016.txt", _TXT.encode("latin-1"))
    by = {(r.code, r.fy, r.country): r for r in rows}
    assert by[("TOTAL", 2016, hs.ALL)].value_pkr_th == 2166846412
    assert by[("01051100", 2016, hs.ALL)].value_pkr_th == 2454
    assert by[("01051100", 2016, "Afghanistan")].quantity == 22900
    assert by[("01051100", 2015, "Oman")].value_pkr_th == 752
    assert meta["01051100"].unit == "NO"
    assert not any(r.country.upper().startswith("PAGE") for r in rows)  # page header isn't a partner
    assert hs.reconcile([r for r in rows if r.fy == 2016]) == []


def test_trade_country_names_and_glued_numbers():
    assert hs.clean_country("U.S.America") == hs.clean_country("U S A") == "United States"
    assert hs.clean_country("BANGLA DESH") == "Bangladesh"
    assert hs.clean_country("NEW ZEALAND") == "New Zealand"
    assert hs.clean_country("Hong Kong S.A.Re.Chi") == "Hong Kong"
    kept = hs._split_glued([(0, 10, "1234567"), (20, 40, "103626890017410764")], [])
    assert [w[2] for w in kept] == ["1234567"]
    assert hs._flow_of("RE - EXPORTS BY COMMODITIES AND COUNTRIES", "export") == "re-export"
    assert hs._flow_of("RE-IMPORTS BY COMMODITIES AND COUNTRIES", "import") == "re-import"


# ---- ISMO power market ---------------------------------------------------------------------------

def test_ismo_month_names_and_prices():
    assert ip.month_of("Market Statistics Nov 23") == date(2023, 11, 30)
    assert ip.month_of("System at a Glance Aug 2026") == date(2026, 8, 31)
    assert ip.month_of("DISCO Consumption 28-02-23") is None
    rows = [(date(2025, 1, 1), h, 10.0 + h) for h in range(24)] + [(date(2025, 1, 2), 0, 50.0)]
    recs = ip.parse_marginal_price(rows)
    daily = {r.obs_date: r.value for r in recs if r.series_id == "energy.power.marginal_price.daily"}
    assert daily == {date(2025, 1, 1): 21.5}  # a part day gets no daily figure
    assert {r.value for r in recs if r.series_id.endswith("daily_max")} == {33.0}


def test_ismo_month_workbook_skips_snapshots_and_broken_demand():
    snap = {"DISCO Consumption": [("DISCO", "Grand Total", "Total Energy (GWh)"), ("FESCO", 28483.5, 28.5)]}
    assert ip.parse_month_workbook(snap, date(2023, 2, 28)) == []
    sheets = {
        "DISCO Consumption": [("DISCO", "Avg", "Total Energy (GWh)"), ("LESCO", 97282.9, 97.28)],
        "Marginal Cost - Demand": [("Hours", "Demand", "Marginal_Cost")] + [(h, 1652.0, 20.0) for h in range(24)],
    }
    recs = ip.parse_month_workbook(sheets, date(2026, 2, 28))
    assert [(r.series_id, r.value) for r in recs] == [("energy.power.disco_consumption", 97.28)]


# ---- merit order (OCR tokens) --------------------------------------------------------------------

def _tok(x0, y, text, w=60):
    return {"box": [[x0, y], [x0 + w, y], [x0 + w, y + 20], [x0, y + 20]], "text": text, "score": 0.99}


def test_merit_order_rows_need_the_checksum():
    width = 2000
    toks = []
    for i, (plant, fuel, other, f, v, spec) in enumerate([
        ("UCH (upto 152,375 MWh)", "Gas", "0.07942", "2.2494", "0.7784", "3.1072"),
        ("Thar Coal Block-l", "Coal", None, "4.1467", "1.2341", "5.3808"),
        ("Engro Power Thar", "Coal", None, "4.2132", "1.5901", "5.9033"),  # misread: 4.2132+1.5901 != 5.9033
        ("Thall NOVA", "Coal", None, "4.2343", "1.5901", "5.8244"),
    ]):
        y = 100 + i * 40
        toks += [_tok(290, y, str(i + 1), 20), _tok(360, y, plant, 400), _tok(850, y, fuel)]
        if other:
            toks.append(_tok(1000, y, other))
        toks += [_tok(1150, y, f), _tok(1300, y, v), _tok(1450, y, spec)]
    rows = mo.parse_page(toks, width)
    assert [(r["plant"], r["fuel"], r["specific"]) for r in rows] == [
        ("UCH (upto 152,375 MWh)", "Gas", 3.1072), ("Thar Coal Block-l", "Coal", 5.3808), ("Thall NOVA", "Coal", 5.8244)]
    assert [r["rank"] for r in rows] == [1, 2, 4]


def test_merit_order_names_dates_and_units():
    assert mo.tidy_name("Jamshoro B-Il (Unit 3)") == "Jamshoro B-II (Unit 3)"
    assert mo.tidy_name("Muzaffargarh B-I (Unit -1)") == "Muzaffargarh B-I (Unit -1)"
    assert mo.signature("747MW GUDDU (CC)") != mo.signature("747MW GUDDU (OC)")
    url = "https://cmsadmin.ismo.gov.pk/storage/uploads/documents/Merit Order 16-04-2025_ismo_p0NW8zH2W6.pdf"
    assert mo.effective_date("Merit Order Based on Revised Fuel Prices (16-04-2024)", url) == date(2025, 4, 16)
    assert mo.effective_date("Economic Merit Order (13-10-2026)") == date(2026, 10, 13)


# ---- census history ------------------------------------------------------------------------------

_GLANCE = """CITY DISTRICT LAHORE AT A GLANCE
Area 1772 Sq.Kms.
Population - 1998 6318745 persons
Male 3328502 (52.68 %)
Female 2990243 ( 47.32 %)
Sex Ratio (males per 100 females) 111.3
Populaltion Density 3565.9 per Sq. Km
Urban Population 5209088 (82.44 %)
Rural Population 1109657 (17.56 %)
Average Household Size 7.2
Literacy Ratio (10 +) 64.7 %
Male 69.05 %
Female 59.68 %
Population - 1981 3544942 persons
Average Annual Growth Rate (1981 - 98) 3.46 %
Total Housing Units 881708
Pacca Housing Units 813772 (92.29 %)
Housing Units having Electricity 845334 (95.87 %)
Housing Units having Piped Water 662725 (75.16 %)
Housing Units using Gas for Cooking 556772 (63.15 %)"""


def test_census_1998_glance():
    recs = ch.parse_glance(_GLANCE, "Lahore.pdf")
    got = {(r.series_id, r.obs_date.year, tuple(sorted((k, v) for k, v in r.dims.items() if k not in ("province", "district")))): r.value
           for r in recs}
    assert got[("regional.census.population", 1998, (("locality", "total"), ("sex", "all")))] == 6318745
    assert got[("regional.census.population", 1981, (("locality", "total"), ("sex", "all")))] == 3544942
    assert got[("regional.census.literacy_rate", 1998, (("locality", "total"), ("sex", "female")))] == 59.68
    assert got[("regional.census.housing_share", 1998, (("amenity", "gas for cooking"),))] == 63.15
    assert recs[0].dims["province"] == "Punjab" and recs[0].dims["district"] == "Lahore"
    assert ch.district_98("", "Raim_Yar_Khan.pdf") == "Rahim Yar Khan"


# ---- PSLM ----------------------------------------------------------------------------------------

def test_pslm_table_columns_and_places():
    def w(x0, y, text):
        return (x0, y, x0 + 7 * len(text), y + 8, text)
    words = [w(56, 10, "TABLE"), w(56, 30, "Pakistan"), w(56, 50, "Punjab"), w(56, 70, "Lahore"), w(56, 90, "Islamabad"),
             w(56, 110, "Attock")]
    cols = [177, 215, 252, 290, 328, 366, 404, 442, 480, 518, 556]
    vals = {30: [79, 65, 72, 62, 34, 48, 68, 46, 57, None, 57], 50: [79, 69, 74, 64, 43, 53, 70, 53, 61, None, 60],
            70: [79, 72, 76, None, None, None, 79, 72, 76, 4, 78], 90: [90, 78, 84, 89, 76, 83, 89, 77, 84, 1, 84],
            110: [82, 63, 72, 79, 49, 64, 80, 53, 66, 11, 64]}
    for y, vs in vals.items():
        for right, v in zip(cols, vs):
            if v is not None:
                words.append((right - 7 * len(str(v)), y, right, y + 8, str(v)))
    recs = pslm.to_records("2.14b", pslm.extract_table(words))
    get = {(r.dims.get("district") or r.dims.get("province") or "Pakistan", r.obs_date.year, r.dims["locality"], r.dims["sex"]): r.value
           for r in recs}
    assert get[("Lahore", 2020, "total", "all")] == 76 and ("Lahore", 2020, "rural", "all") not in get
    assert get[("Lahore", 2015, "total", "all")] == 78
    assert get[("Attock", 2020, "rural", "female")] == 49
    assert get[("Islamabad", 2020, "total", "all")] == 84
    attock = next(r for r in recs if r.dims.get("district") == "Attock")
    assert attock.dims["province"] == "Punjab"  # Islamabad's row didn't switch the province
    assert pslm.table_number("Table-2.14b-Adult-Literacy.pdf") == "2.14b"


def test_fuel_price_in_usd():
    from ingestion.jobs.derive_indicators import in_usd_month_end
    changes = [(date(2024, 1, 16), 280.0), (date(2024, 2, 16), 300.0)]
    fx = [(date(2024, 1, 31), 280.0), (date(2024, 2, 29), 279.0), (date(2024, 3, 31), 278.0)]
    out = in_usd_month_end(changes, fx)
    assert out == [(date(2024, 1, 31), 1.0), (date(2024, 2, 29), round(300 / 279, 4)), (date(2024, 3, 31), round(300 / 278, 4))]
