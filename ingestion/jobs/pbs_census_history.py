"""pbs_census_history - Pakistan's 1981, 1998 and 2017 censuses by province, district and tehsil.

Extends the `regional.census.*` series (Census 2023: `pbs_census_2023`) back in time, from PBS's
census archive (pbs.gov.pk/censusarchive):

- Census 2017, Table 1 (one workbook per province + Islamabad + FATA: Table01p*.xls, Table01d.xls):
  area, population by sex, sex ratio, density, urban proportion, household size, the 1998 population
  re-tabulated to 2017 boundaries and the 1998-2017 annual growth rate — for every division, district
  and tehsil, each with RURAL and URBAN rows. Same layout as the 2023 table, so the geography logic
  is shared with `pbs_census_2023`.
- Census 1998, District Census Reports "at a glance" (one PDF per 1998 district + NCR-Pakistan):
  area, population by sex, rural/urban, sex ratio, density, household size, literacy (10+) by sex,
  the 1981 population and 1981-98 growth, housing units and their pacca / electricity / piped-water /
  gas shares.

Dates: each census at 31 December of its year (2017-12-31, 1998-12-31, 1981-12-31), like 2023.
Boundaries: values are as enumerated, on that census's own district map (1998 had ~106 districts,
2017 ~135, 2023 ~160), so a district created later starts at the census that first counted it.
The 1998 figures that PBS re-tabulated to 2017 boundaries go to `regional.census.population_1998`
(dated 2017), mirroring `population_2017` for 2023. FATA's 2017 agencies are listed under province
"Khyber Pakhtunkhwa" (merged in 2018) with their district names, so they line up with 2023.
PBS blocks the VPS: workstation job (`dq_fetch_parse.py job pbs_census_history`).
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.jobs.pbs_census_2023 import _Geo, _place, _title

ARCHIVE_URL = "https://www.pbs.gov.pk/censusarchive/"
WP = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/"
T17 = ["Table01p.xls", "Table01p-3.xls", "Table01p-4.xls", "Table01p-5.xls", "Table01p-6.xls", "Table01d.xls"]
D17, D98, D81 = date(2017, 12, 31), date(1998, 12, 31), date(1981, 12, 31)
_SKIP = re.compile(r"^(NCR|Table\d|Kpk_Table|administrative|contents|publication|national|pcr_|rcr_|sailent|"
                   r".*_district_wise|.*_tehsil|fata_|balochistan_|kp_|punjab_|sindh_)", re.I)
_PROV = {"PUNJAB": "Punjab", "SINDH": "Sindh", "KHYBER PAKHTUNKHWA": "Khyber Pakhtunkhwa",
         "BALOCHISTAN": "Balochistan", "FATA": "FATA", "ISLAMABAD DISTRICT": "Islamabad"}

# 1998 district names (report titles / file names) → the names the 2017/2023 tables use.
ALIASES_98 = {"Jaccobabad": "Jacobabad", "Raim Yar Khan": "Rahim Yar Khan", "Shaiwal": "Sahiwal",
              "Kohlui": "Kohlu", "Peshaware": "Peshawar", "D G Khan": "Dera Ghazi Khan", "D. G. Khan": "Dera Ghazi Khan",
              "D I Khan": "Dera Ismail Khan", "D. I. Khan": "Dera Ismail Khan", "T T Singh": "Toba Tek Singh",
              "T. T. Singh": "Toba Tek Singh", "Malakand Protected Area": "Malakand", "Nawabshah": "Shaheed Benazirabad",
              "Mirpur Khas": "Mirpur Khas", "Mirpurkhas": "Mirpur Khas", "Umerkot": "Umer Kot", "Umarkot": "Umer Kot", "Dera Ismial Khan": "Dera Ismail Khan", "Shikapur": "Shikarpur", "Killa Abdullah": "Killa Abdullah", "Bolan": "Kachhi",
              "Mandi Bahauddin": "Mandi Bahauddin", "Lakki Marwat": "Lakki Marwat"}
# 1998 districts' provinces (the archive's file list carries none).
_P98: dict[str, str] = {}
_P98_LIST = {
    "Punjab": ["Attock", "Bahawalnagar", "Bahawalpur", "Bhakkar", "Chakwal", "Dera Ghazi Khan", "Faisalabad",
               "Gujranwala", "Gujrat", "Hafizabad", "Jhang", "Jhelum", "Kasur", "Khanewal", "Khushab", "Lahore",
               "Layyah", "Lodhran", "Mandi Bahauddin", "Mianwali", "Multan", "Muzaffargarh", "Narowal", "Okara",
               "Pakpattan", "Rahim Yar Khan", "Rajanpur", "Rawalpindi", "Sahiwal", "Sargodha", "Sheikhupura",
               "Sialkot", "Toba Tek Singh", "Vehari"],
    "Sindh": ["Badin", "Dadu", "Ghotki", "Hyderabad", "Jacobabad", "Karachi", "Khairpur", "Larkana", "Mirpur Khas",
              "Naushahro Feroze", "Shaheed Benazirabad", "Sanghar", "Shikarpur", "Sukkur", "Tharparkar", "Thatta",
              "Umer Kot"],
    "Khyber Pakhtunkhwa": ["Abbottabad", "Bannu", "Batagram", "Buner", "Charsadda", "Chitral", "Dera Ismail Khan",
                           "Hangu", "Haripur", "Karak", "Kohat", "Kohistan", "Lakki Marwat", "Lower Dir", "Malakand",
                           "Mansehra", "Mardan", "Nowshera", "Peshawar", "Shangla", "Swabi", "Swat", "Tank",
                           "Upper Dir"],
    "Balochistan": ["Awaran", "Barkhan", "Kachhi", "Chagai", "Dera Bugti", "Gwadar", "Jaffarabad", "Jhal Magsi",
                    "Kalat", "Kech", "Kharan", "Khuzdar", "Killa Abdullah", "Killa Saifullah", "Kohlu", "Lasbela",
                    "Loralai", "Mastung", "Musakhel", "Nasirabad", "Panjgur", "Pishin", "Quetta", "Sibi", "Zhob",
                    "Ziarat"],
    "Islamabad": ["Islamabad"],
}
for _p, _names in _P98_LIST.items():
    for _n in _names:
        _P98[_n] = _p

_C = dict(module="regional", frequency="census", source="PBS", source_url=ARCHIVE_URL)
CATALOG = [
    dict(id="regional.census.population_1998", name="Population in 1998 (Census 1998, as tabulated in 2017)",
         unit="count", min_value=0, max_value=4e8, dimensions={"locality": ["total", "rural", "urban"]},
         description="Population of each province, district and tehsil in 1998, re-tabulated by PBS to the 2017 "
                     "census boundaries (dimension locality: total, rural, urban), dated with the 2017 census.", **_C),
    dict(id="regional.census.housing_units", name="Housing units (census)", unit="count", min_value=0, max_value=1e8,
         description="Number of housing units in each district at the 1998 census.", **_C),
    dict(id="regional.census.housing_share", name="Share of housing units with each amenity (census)",
         unit="percent", min_value=0, max_value=100, dimensions={"amenity": ["electricity"]},
         description="Share of housing units in each district at the 1998 census that are pacca, have electricity, "
                     "have piped water or use gas for cooking (dimension amenity).", **_C),
]


def _f(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _xls_rows(content: bytes) -> list[tuple[str, list]]:
    """(label, numbers) rows of a 2017 Table 1 workbook. Pure."""
    import xlrd

    sh = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    out = []
    for i in range(sh.nrows):
        row = sh.row_values(i)
        label = str(row[0]).strip()
        if not label or _f(label) is not None:
            continue
        nums = [_f(v) for v in row[1:12]]
        while nums and nums[-1] is None:
            nums.pop()
        out.append((re.sub(r"\s+", " ", label.upper()), nums))
    return out


_DIVISION = re.compile(r"\bDIV(ISION)?\b|^DIVISION-\d")


def _district_label(label: str, fata: bool) -> str:
    lab = re.sub(r"\s+AGENCY$", " DISTRICT", label)
    if fata and lab.startswith("FR "):  # Frontier Regions: district-level units
        lab = "F.R. " + lab[3:].replace("D.I.KHAN", "DERA ISMAIL KHAN") + " DISTRICT"
    return lab


def parse_2017(content: bytes) -> list[Record]:
    """A 2017 Table 1 workbook → records. Pure."""
    rows = _xls_rows(content)
    first = next((lab for lab, n in rows if n and lab not in ("RURAL", "URBAN")), "")
    province = _PROV.get(first)
    if province is None:
        raise ValueError(f"unrecognised 2017 Table 1 workbook starting {first!r}")
    fata = province == "FATA"
    # The province workbook lists only province / division / district rows, so every other row is a
    # district even without the suffix (MALAKAND PROTECTED AREA, the FATA agencies and FRs).
    home = "Khyber Pakhtunkhwa" if fata else province
    names = {_place(_district_label(lab, fata), home)[0] for lab, n in rows
             if len(n) >= 11 and lab not in ("RURAL", "URBAN", "FATA") and not _DIVISION.search(lab)}
    geo = _Geo(home, names - {home})
    out: list[Record] = []
    cur = None
    for label, nums in rows:
        if len(nums) < 11:
            continue
        nums = nums[:11]
        if label in ("RURAL", "URBAN"):  # same columns as the total row; area/density/urban blank
            loc = label.lower()
        else:
            if fata and label == "FATA":
                cur = None  # FATA as a whole has no counterpart after the merger
                continue
            if _DIVISION.search(label):
                continue
            cur = geo.dims(_district_label(label, fata))
            if cur is None:
                continue
            loc = "total"
        sqkm, pop, male, female, trans, ratio, dens, urban, hh, p98, gr = nums
        if cur is None:
            continue
        for sid, v in (("area", sqkm), ("density", dens), ("urban_share", urban)):
            if v is not None:
                out.append(Record(f"regional.census.{sid}", D17, round(v, 4), dict(cur)))
        d = {**cur, "locality": loc}
        for sex, v in (("all", pop), ("male", male), ("female", female), ("transgender", trans)):
            if v is not None:
                out.append(Record("regional.census.population", D17, v, {**d, "sex": sex}))
        for sid, v in (("sex_ratio", ratio), ("household_size", hh), ("growth_rate", gr)):
            if v is not None:
                out.append(Record(f"regional.census.{sid}", D17, round(v, 4), dict(d)))
        if p98 is not None:
            out.append(Record("regional.census.population_1998", D17, p98, dict(d)))
            if "district" not in cur:  # province boundaries are unchanged: the 1998 count itself
                out.append(Record("regional.census.population", D98, p98, {**d, "sex": "all"}))
    return out


_GLANCE = re.compile(r"(?:CITY\s+)?DISTRICT\s+(.+?)\s+AT A GLANCE|(.+?)\s+DISTRICT\s+AT A GLANCE|(.+?)\s+AT A GLANCE")


def _pct_pair(text: str, key: str) -> tuple[float | None, float | None]:
    m = re.search(re.escape(key) + r"\s+([\d,.]+)\s*(?:persons)?\s*(?:\(\s*([\d.]+)\s*%\s*\))?", text)
    if not m:
        return None, None
    return _f(m.group(1)), _f(m.group(2))


def district_98(title: str, filename: str) -> str:
    m = _GLANCE.search(title.upper())
    raw = next((g for g in (m.groups() if m else ()) if g), None) or re.sub(r"[_-]", " ", filename.rsplit(".", 1)[0])
    raw = re.sub(r"\b(CITY|DISTRICT)\b", "", raw.upper()).strip()
    name = _title(re.sub(r"\s+", " ", raw))
    return ALIASES_98.get(name, name)


def parse_glance(text: str, filename: str) -> list[Record]:
    """A 1998 District Census Report 'at a glance' page → records. Pure."""
    title = next((ln for ln in text.splitlines() if "AT A GLANCE" in ln.upper()), "")
    national = "NCR" in filename.upper() or "PAKISTAN" in title.upper()
    if national:
        dims: dict = {}
    else:
        name = district_98(title, filename)
        prov = _P98.get(name)
        if prov is None:
            raise ValueError(f"no province for 1998 district {name!r} ({filename})")
        dims = {"province": prov, "district": name} if prov != "Islamabad" else {"province": "Islamabad",
                                                                                  "district": "Islamabad"}
    out: list[Record] = []

    def add(sid, d, v, extra=None):
        if v is not None:
            out.append(Record(f"regional.census.{sid}", d, v, {**dims, **(extra or {})}))

    area = _f((re.search(r"Area\s+([\d,.]+)", text) or [None, None])[1])
    pop98, _ = _pct_pair(text, "Population - 1998")
    male, _ = _pct_pair(text, "Male")
    female, _ = _pct_pair(text, "Female")
    urban, ushare = _pct_pair(text, "Urban Population")
    rural, _ = _pct_pair(text, "Rural Population")
    add("area", D98, area)
    total = {"locality": "total"}
    add("population", D98, pop98, {**total, "sex": "all"})
    add("population", D98, male, {**total, "sex": "male"})
    add("population", D98, female, {**total, "sex": "female"})
    add("population", D98, urban, {"locality": "urban", "sex": "all"})
    add("population", D98, rural, {"locality": "rural", "sex": "all"})
    add("urban_share", D98, ushare)
    for sid, key in (("sex_ratio", r"Sex Ratio \(males per 100 females\)"), ("density", r"Popul\w*\s+Density"),
                     ("household_size", r"Average Household Size")):
        m = re.search(key + r"\s+([\d,.]+)", text)
        add(sid, D98, _f(m.group(1)) if m else None, total if sid != "density" else None)
    lit = re.search(r"Literacy Ratio \(10 ?\+\)\s+([\d.]+)\s*%\s+Male\s+([\d.]+)\s*%\s+Female\s+([\d.]+)", text)
    if lit:
        add("literacy_rate", D98, _f(lit.group(1)), {**total, "sex": "all"})
        add("literacy_rate", D98, _f(lit.group(2)), {**total, "sex": "male"})
        add("literacy_rate", D98, _f(lit.group(3)), {**total, "sex": "female"})
    p81 = re.search(r"Population - 1981\s+([\d,]+)", text)
    add("population", D81, _f(p81.group(1)) if p81 else None, {**total, "sex": "all"})
    g = re.search(r"Growth Rate \(1981\s*-\s*98\)\s+([\d.]+)", text)
    add("growth_rate", D98, _f(g.group(1)) if g else None, total)
    hu = re.search(r"Total Housing Units\s+([\d,]+)", text)
    add("housing_units", D98, _f(hu.group(1)) if hu else None)
    for amenity, key in (("pacca", "Pacca Housing Units"), ("electricity", "Housing Units having Electricity"),
                         ("piped water", "Housing Units having Piped Water"),
                         ("gas for cooking", "Housing Units using Gas for Cooking")):
        _, share = _pct_pair(text, key)
        add("housing_share", D98, share, {"amenity": amenity})
    if pop98 is None:
        raise ValueError(f"no 1998 population in {filename}")
    return out


class PbsCensusHistoryJob(IngestionJob):
    name = "pbs_census_history"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        html = self.http_get(ARCHIVE_URL).decode("utf-8", "replace")
        names = sorted({u.rsplit("/", 1)[1] for u in re.findall(r'href="([^"]+/wp-content/uploads/2020/07/[^"]+\.pdf)"', html)})
        dcr = [n for n in names if not _SKIP.match(n)]
        out = [FetchedFile(n, self.http_get(WP + n), date.today()) for n in T17]
        out += [FetchedFile("dcr_" + n, self.http_get(WP + n), date.today()) for n in dcr]
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        if f.filename.endswith(".xls"):
            return parse_2017(f.content)
        import pdfplumber

        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages[:1])
        return parse_glance(text, f.filename[4:])
