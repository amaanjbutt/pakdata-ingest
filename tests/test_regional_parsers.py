"""Parsers for the 2026-10-16 granular sources: NEPRA plant generation, Census 2023,
Economic Census 2023, Agricultural Census 2024, LFS and HIES."""
import io
from datetime import date

import openpyxl

from ingestion.jobs import nepra_generation as nepra
from ingestion.jobs.pbs_agri_census import KEYS, parse_by_unit, parse_farms
from ingestion.jobs.pbs_census_2023 import district_names, parse_table1
from ingestion.jobs.pbs_economic_census import parse_districts
from ingestion.jobs.pbs_hies import parse_summary_tables
from ingestion.jobs.pbs_lfs import parse_key_tables


def _nepra_sheet(fy: str, name: str, tech: str = "THERMAL") -> str:
    months = "".join(f"<td>{10 + i}.00</td><td>{100 + i}.50</td>" for i in range(12))
    return (f"<table><tr><td>S.No</td><td>Name of Companies</td><td>FY {fy}</td></tr>"
            f"<tr><td>1</td><td>{name}</td><td>{tech}</td><td>RFO</td><td>1,362</td><td>1,350</td>{months}"
            "<td>15.00</td><td>1,266.00</td></tr></table>")


def test_nepra_sheet_and_plant_renames():
    y0, plants = nepra.parse_sheet(_nepra_sheet("2023-24", "AES Lalpir power limited."))
    assert y0 == 2023 and plants[0]["installed"] == 1362
    assert plants[0]["months"][0] == (date(2023, 7, 31), 10.0, 100.5)
    assert plants[0]["months"][-1][0] == date(2024, 6, 30)
    recs = nepra.to_records([nepra.parse_sheet(_nepra_sheet("2023-24", "AES Lalpir power limited.")),
                             nepra.parse_sheet(_nepra_sheet("2024-25", "AES Lalpir Power (Pvt.) Ltd"))])
    gen = [r for r in recs if r.series_id == "energy.generation.plant"]
    assert {r.dims["plant"] for r in gen} == {"Lalpir"}  # both spellings -> the plants_pk registry name
    assert len(gen) == 24
    src = [r for r in recs if r.series_id == "energy.generation.by_source"]
    assert {r.dims["source"] for r in src} == {"thermal"}


CENSUS_DISTRICTS = """TABLE 1 : AREA, POPULATION ... CENSUS-2023, SINDH
SINDH 140,914 55,696,147 29,014,424 26,677,501 4,222 108.76 395.25 53.97 5.6 47,854,510 2.57
RURAL 25,639,408 13,245,373 12,393,405 630 106.87 5.4 23,021,876 1.82
BADIN DISTRICT 6,858 1,947,081 1,012,854 934,153 74 108.42 283.91 22.08 4.8 1,804,958 1.27
URBAN 429849 222,826 206,984 39 107.65 5.0 390,344 1.62
BADIN TALUKA 1,816 490,386 255,677 234,691 18 108.94 270.04 29.07 4.8 454,576 1.28
KAMBAR SHAHDAD KOT
5,475 1,514,869 791,116 723,710 43 109.31 276.69 27.85 5.6 1,338,035 2.10
DISTRICT
KAMBAR ALI KHAN TALUKA 2,260 448,990 234,516 214,459 15 109.35 198.67 25.01 5.7 393,374 2.23
KHAIRPUR NATHAN SHAH 1,000 300,000 150,000 150,000 1 100.00 300.00 10.00 5.0 290,000 0.50
SOUTH WAZIRISTAN DISTRICT 6,620 888,675 459,531 429,130 14 107.08 134.24 4.9 675,215 4.70
"""
CENSUS_LIST = """SINDH 140,914 55,696,147 29,014,424 26,677,501 4,222 108.76 395.25 53.97 5.6 47,854,510 2.57
HYDERABAD DIVISION 48,670 11,659,246 6,031,701 5,627,004 541 107.19 239.56 37.28 5.1 10,596,049 1.61
BADIN DISTRICT 6,858 1,947,081 1,012,854 934,153 74 108.42 283.91 22.08 4.8 1,804,958 1.27
KAMBAR SHAHDAD KOT DISTRICT 5,475 1,514,869 791,116 723,710 43 109.31 276.69 27.85 5.6 1,338,035 2.10
SOUTH WAZIRISTAN DISTRICT 6,620 888,675 459,531 429,130 14 107.08 134.24 4.9 675,215 4.70
"""


def test_census_table1_geography():
    districts = district_names(CENSUS_LIST, "Sindh")
    assert districts == {"Badin", "Kambar Shahdad Kot", "South Waziristan"}
    recs = parse_table1(CENSUS_DISTRICTS, "Sindh", districts)
    pop = {(tuple(sorted(r.dims.items()))): r.value for r in recs if r.series_id == "regional.census.population"}
    assert pop[(("locality", "total"), ("province", "Sindh"), ("sex", "all"))] == 55696147
    assert pop[(("district", "Badin"), ("locality", "urban"), ("province", "Sindh"), ("sex", "male"))] == 222826
    # a label wrapped around its numbers is still a district
    assert pop[(("district", "Kambar Shahdad Kot"), ("locality", "total"), ("province", "Sindh"), ("sex", "all"))] == 1514869
    # an unsuffixed sub-unit stays under its district
    other = [r for r in recs if r.dims.get("subdistrict") == "Khairpur Nathan Shah"]
    assert other and other[0].dims["district"] == "Kambar Shahdad Kot" and other[0].dims["unit_type"] == "other"
    # no urban population: the blank urban-share cell doesn't shift the columns
    sw = {r.series_id: r.value for r in recs if r.dims.get("district") == "South Waziristan" and "locality" not in r.dims}
    assert sw == {"regional.census.area": 6620, "regional.census.density": 134.24}


def _xlsx(rows) -> bytes:
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_economic_census_districts():
    content = _xlsx([["Table 1: Establishments of Punjab Districts"], ["RAHIM YAR KHAN DISTRICT"],
                     ["Sr. No.", "Description", "PSIC Codes", "No of Establishments", "Workforce"],
                     [None, None, None, 68514, 206271],
                     [1, "Agriculture, forestry and fishing", "1 to 3", 7033, 20211],
                     [2, "others", "99", 5, 9]])
    recs = parse_districts(content, "Punjab")
    assert {(r.series_id, r.dims["sector"], r.value) for r in recs} >= {
        ("regional.econ_census.establishments", "All sectors", 68514),
        ("regional.econ_census.workforce", "Agriculture, forestry and fishing", 20211),
        ("regional.econ_census.establishments", "Others", 5)}
    assert recs[0].dims["district"] == "Rahim Yar Khan"


def test_agri_census_units_and_farms():
    rows = [["ADMINISTRATIVE UNIT", "ALL TRACTORS"], ["PUNJAB", 777404.2, 700000.1, 77000.0, 404.1],
            ["LAHORE DIVISION", 1, 1, 0, 0], ["LAHORE DISTRICT", 9000.4, 8000.0, 1000.4, 0],
            ["CHOLISTAN AREA", 10.0, 10.0, 0, 0]]
    recs = parse_by_unit(rows, "regional.agri_census.tractors", KEYS["tractors"])
    assert {(r.dims.get("district"), r.dims["ownership"], r.value) for r in recs} >= {
        (None, "all", 777404), ("Lahore", "joint", 1000), ("Cholistan Area", "all", 10)}
    assert not any(r.dims.get("district") == "Lahore Division" for r in recs)
    farms = parse_farms([["KHYBER PAKHTUNKHWA"], ["PRIVATE-FARMS TOTAL", 3576609.6, 100.0, 8836751.5, 100.0, 7236338.5, 100.0],
                         ["UNDER 1.0", 1573727.1, 44.0, 1229930.6, 13.9, 1020901.5, 14.1]])
    assert {(r.series_id, r.dims["farm_size"], r.value) for r in farms} >= {
        ("regional.agri_census.farms", "all", 3576610), ("regional.agri_census.cultivated_area", "under 1.0", 1020902)}


LFS = """Table 3.3 Key Labour Market Indicators by Sex and Provinces 2024-25 (19th ICLS) ......18
Table 3.3 Key Labour Market Indicators by Sex and Provinces 2024-25 (19th ICLS)
Provinces Employed Unemployed Labour Age UR EPR LFPR
Total 77.2 5.9 83.1 96.5 179.6 7.1 43.0 46.3
KP 11.2 1.2 12.5 17.8 30.3 9.6 37.2 41.2
Female 17.8 2.1 19.9 67.7 87.6 10.5 20.4 22.7
Punjab 12.4 1.3 13.7 35.0 48.6 9.6 25.4 28.1
Table 3.4 Key Labour Market Indicators by Sex and Provinces 2020-21 (13th ICLS)
Both Sexes 67.3 4.5 71.8 88.1 159.8 6.3 42.1 44.9
"""


def test_lfs_key_tables():
    recs = parse_key_tables(LFS)
    ur = {(r.obs_date.year, r.dims.get("province"), r.dims["sex"]): r.value
          for r in recs if r.series_id == "regional.lfs.unemployment_rate"}
    assert ur == {(2025, None, "all"): 7.1, (2025, "Khyber Pakhtunkhwa", "all"): 9.6,
                  (2025, None, "female"): 10.5, (2025, "Punjab", "female"): 9.6, (2021, None, "all"): 6.3}


HIES = """TABLE 3.2.C AVERAGE HOUSEHOLD SIZE, BY PROVINCES AND REGION
AREA 2018-19 2024-25
Total 6.24 5.98
Punjab 5.78 5.77
TABLE 3.3 AVERAGE NUMBERS OF EARNERS PER HOUSEHOLD BY PROVINCE AND
Total 1.86 1.72
TABLE: 3.5.A AVERAGE MONTHLY HOUSEHOLD INCOME (RS.) BY QUINTILES AND AREAS
1st 24365 22819 23192 42412 41714 41851 80.45
TOTAL 53010 34520 41545 96767 72157 82179 97.81
TABLE: 3.6.A AVERAGE MONTHLY HOUSEHOLD CONSUMPTION EXPENDITURE (RS.) BY
TOTAL 47362 30908 37159 95533 67894 79150 113.00
TABLE 3.6.B PER CAPITA MONTHLY CONSUMPTION EXPENDITURE (RS.) BY QUINTILES
TOTAL 7928 4832 5959 16795 10991 13240 122.19
"""


def test_hies_summary_tables():
    recs = parse_summary_tables(HIES)
    size = {(r.obs_date.year, tuple(r.dims.items())) : r.value for r in recs if r.series_id == "regional.hies.household_size"}
    assert size == {(2019, (("locality", "total"),)): 6.24, (2025, (("locality", "total"),)): 5.98,
                    (2019, (("province", "Punjab"),)): 5.78, (2025, (("province", "Punjab"),)): 5.77}
    inc = {(r.obs_date.year, r.dims["locality"], r.dims["quintile"]): r.value
           for r in recs if r.series_id == "regional.hies.household_income"}
    assert inc[(2025, "total", "all")] == 82179 and inc[(2019, "urban", "1")] == 24365


def test_plant_registry_splits_operating_details():
    from ingestion.plants_pk import canon_plant
    assert canon_plant("UCH (upto 152,375 MWh)") == {"plant": "UCH", "tranche": "up to 152,375 MWh"}
    assert canon_plant("Guddu(OC) B-II (Unit 5-10)") == {"plant": "Guddu", "config": "OC",
                                                         "unit": "Block II, Units 5-10"}
    assert canon_plant("(NPPMCL) - Balloki") == {"plant": "Balloki (NPPMCL)"}
    assert canon_plant("NPPMC - Baloki (CC)") == {"plant": "Balloki (NPPMCL)", "config": "CC"}
    assert canon_plant("Thar Coal Block-I") == {"plant": "Thar Coal Block-I"}
    assert canon_plant("Karot Power Company (Pvt.) Ltd. (KPCL)") == canon_plant("HPS Karot 720MW") == {"plant": "Karot"}
    assert canon_plant("Brand New Power (Pvt.) Limited")["plant"] == "Brand New Power"   # fallback tidy
