"""pbs_census_2023 - 7th Population & Housing Census (2023), district and sub-district tables.

Source: PBS census tables, one PDF per province,
    https://www.pbs.gov.pk/wp-content/uploads/census_tables/tables/table_{N}_{province}_districts.pdf
(`table_{N}_islamabad.pdf` for ICT; Balochistan's Table 12 is `…_balochistan_district.pdf`).

Table 1 — area, population by sex, sex ratio, density, urban proportion, household size,
2017 population and the 2017-2023 annual growth rate — for the province, every district and
every sub-district unit (tehsil / taluka / sub-division / sub-tehsil), each followed by its
RURAL and URBAN rows:

    ATTOCK DISTRICT 6,857 2,170,423 1,089,655 1,080,687 81 100.83 316.53 28.75 6.1 1,886,378 2.37
    RURAL 1,546,439 775,779 770,642 18 100.67 6.0 1,395,470 1.73

Table 12 — literacy rate (age 10+) and out-of-school children (age 5-16), by sex, for total /
rural / urban; read by column position because the PDF splits each number's first digit off.

Geography dims: `province`, `district` (absent on province rows), `subdistrict` + `unit_type`
(tehsil, taluka, sub-division, sub-tehsil — Balochistan nests sub-tehsils inside tehsils, so
don't sum sub-district rows), `locality` (total | rural | urban), `sex` where it applies.
Dated 2023-12-31 (enumeration ran March-May 2023). PBS blocks the VPS, so this runs from a
workstation (`scripts/dq_fetch_parse.py job pbs_census_2023`); the census doesn't change.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record

BASE = "https://www.pbs.gov.pk/wp-content/uploads/census_tables/tables"
PROVINCES = {"punjab": "Punjab", "sindh": "Sindh", "kp": "Khyber Pakhtunkhwa", "balochistan": "Balochistan",
             "islamabad": "Islamabad"}
FILES = {(1, p): f"{BASE}/table_1_{p}_districts.pdf" for p in ("punjab", "sindh", "kp", "balochistan")}
# Province-level Table 1 lists only the province, divisions and districts — the authoritative
# district list (the district-level file leaves "DISTRICT" off a few labels).
DISTRICT_LISTS = {p: f"{BASE}/table_1_{p}_province.pdf" for p in ("punjab", "sindh", "kp", "balochistan")}
FILES |= {(12, p): f"{BASE}/table_12_{p}_districts.pdf" for p in ("punjab", "sindh", "kp")}
FILES |= {(1, "islamabad"): f"{BASE}/table_1_islamabad.pdf", (12, "islamabad"): f"{BASE}/table_12_islamabad.pdf",
          (12, "balochistan"): f"{BASE}/table_12_balochistan_district.pdf"}
OBS_DATE = date(2023, 12, 31)
SOURCE_URL = "https://www.pbs.gov.pk/census/"

_C = dict(module="regional", frequency="census", source="PBS", source_url=SOURCE_URL)
_GEO = ("by province, district and sub-district (tehsil / taluka / sub-division), "
        "total, rural and urban — 7th Population & Housing Census 2023")
CATALOG = [
    dict(id="regional.census.population", name="Population (Census 2023)", unit="count", min_value=0,
         max_value=300e6, description=f"Population by sex (dimension `sex`: all, male, female, transgender), {_GEO}.",
         **_C),
    dict(id="regional.census.population_2017", name="Population in 2017 (Census 2017, as tabulated in 2023)",
         unit="count", min_value=0, max_value=300e6,
         description=f"2017 census population of the same areas, {_GEO}.", **_C),
    dict(id="regional.census.growth_rate", name="Average annual population growth, 2017-2023", unit="percent",
         # urban areas redrawn since 2017 grow fast on paper (Ziarat urban: 56.46% a year, as PBS prints it)
         min_value=-100, max_value=200,
         description=f"Average annual population growth rate 2017-2023, {_GEO}.", **_C),
    dict(id="regional.census.area", name="Area (sq km)", unit="sq_km", min_value=0, max_value=1e6,
         description=f"Land area in square kilometres, {_GEO}.", **_C),
    dict(id="regional.census.density", name="Population density", unit="per_sq_km", min_value=0, max_value=1e6,
         description=f"Persons per square kilometre, {_GEO}.", **_C),
    dict(id="regional.census.urban_share", name="Urban population share", unit="percent", min_value=0,
         max_value=100, description=f"Urban share of the population, {_GEO}.", **_C),
    dict(id="regional.census.sex_ratio", name="Sex ratio (males per 100 females)", unit="ratio", min_value=0,
         max_value=500, description=f"Males per 100 females, {_GEO}.", **_C),
    dict(id="regional.census.household_size", name="Average household size", unit="persons", min_value=0,
         max_value=50, description=f"Average persons per household, {_GEO}.", **_C),
    dict(id="regional.census.literacy_rate", name="Literacy rate, age 10+ (Census 2023)", unit="percent",
         min_value=0, max_value=100,
         description=f"Literate share of the population aged 10 and over, by sex, {_GEO}.", **_C),
    dict(id="regional.census.out_of_school", name="Out-of-school children, age 5-16 (Census 2023)", unit="count",
         min_value=0, max_value=50e6, description=f"Children aged 5-16 not in school, by sex, {_GEO}.", **_C),
]

_UNIT_TYPES = {"DISTRICT": "district", "TEHSIL": "tehsil", "TALUKA": "taluka", "SUB-DIVISION": "sub-division",
               "SUB-TEHSIL": "sub-tehsil", "DIVISION": "division"}
_LINE = re.compile(r"^([A-Z][A-Z0-9 .()&'/@-]*?[A-Z.)])\s+((?:-|[\d,]+(?:\.\d+)?)(?:\s+(?:-|[\d,]+(?:\.\d+)?))+)$")


def _num(s: str) -> float | None:
    return None if s == "-" else float(s.replace(",", ""))


def _title(name: str) -> str:
    return " ".join(w.capitalize() if not re.match(r"^[A-Z]\.", w) else w for w in name.split())


# Table 1's spellings → the names PBS uses in its other tables
_ALIASES = {"Tando Ahyar": "Tando Allahyar", "Malakand Protected Area": "Malakand"}


def _place(name: str, province: str) -> tuple[str, str | None]:
    """(clean name, unit type) from a row label like 'ATTOCK DISTRICT' / 'BADIN TALUKA'."""
    for suffix in sorted(_UNIT_TYPES, key=len, reverse=True):
        if name.endswith(" " + suffix):
            clean = _title(name[: -len(suffix) - 1])
            return _ALIASES.get(clean, clean), _UNIT_TYPES[suffix]
    if name.replace(" ", "") == province.upper().replace(" ", ""):
        return province, "province"
    clean = _title(name)
    return _ALIASES.get(clean, clean), None  # Karachi towns etc. carry no suffix: a sub-district unit


class _Geo:
    """Tracks where a row sits: province → district → sub-district."""

    def __init__(self, province: str, districts: set[str] | None = None):
        self.province, self.district, self.districts = province, None, districts

    def dims(self, label: str) -> dict | None:
        name, kind = _place(label, self.province)
        if self.districts is not None and kind in (None, "district"):
            # a tehsil may share its district's name (unsuffixed KILLA ABDULLAH right after the district)
            kind = "district" if name in self.districts and name != self.district else None
        if kind == "province":
            self.district = None
            return {"province": self.province}
        if kind == "division":
            return None  # divisions only group districts; skipped
        if kind == "district" or (self.province == "Islamabad" and self.district is None):
            self.district = name
            return {"province": self.province, "district": name}
        if self.district is None:
            return None
        return {"province": self.province, "district": self.district, "subdistrict": name,
                "unit_type": kind or "other"}


_LABEL_ONLY = re.compile(r"^[A-Z][A-Z .()&'/@-]*$")
_NUMS_ONLY = re.compile(r"^(?:-|[\d,]+(?:\.\d+)?)(?:\s+(?:-|[\d,]+(?:\.\d+)?))+$")


def _rows1(text: str) -> list[tuple[str, list]]:
    raw = [re.sub(r"([A-Z)])(?=\d)", lambda m: m.group(1) + " ", ln.strip())  # "DISTRICT1,423"
           for ln in text.splitlines()]
    lines, i = [], 0
    while i < len(raw):
        # a long label wraps around its numbers: "KAMBAR SHAHDAD KOT" / "5,475 1,514,869 ..." / "DISTRICT"
        if _LABEL_ONLY.match(raw[i]) and i + 1 < len(raw) and _NUMS_ONLY.match(raw[i + 1]):
            tail = raw[i + 2] if i + 2 < len(raw) and raw[i + 2] in _UNIT_TYPES else ""
            lines.append(f"{raw[i]} {tail}".strip() + " " + raw[i + 1])
            i += 3 if tail else 2
            continue
        lines.append(raw[i])
        i += 1
    out = []
    for m in map(_LINE.match, lines):
        if not m:
            continue
        label, nums = m.group(1), [_num(x) for x in m.group(2).split()]
        if label not in ("RURAL", "URBAN") and len(nums) == 10:
            nums.insert(7, None)  # no urban population (e.g. SOUTH WAZIRISTAN): the urban-share cell is blank
        out.append((label, nums))
    return out


def district_names(text: str, province: str) -> set[str]:
    """District names from a province-level Table 1 (province, division and district rows only —
    a few districts carry no suffix, e.g. MALAKAND PROTECTED AREA). Pure."""
    return {_place(label, province)[0] for label, nums in _rows1(text)
            if len(nums) == 11 and _place(label, province)[1] in ("district", None)}


def parse_table1(text: str, province: str, districts: set[str] | None = None) -> list[Record]:
    out: list[Record] = []
    geo, cur = _Geo(province, districts), None
    for label, nums in _rows1(text):
        if label in ("RURAL", "URBAN"):
            if cur is None or len(nums) != 8:
                continue
            area = label.lower()
            pop, male, female, trans, ratio, hh, p17, gr = nums
        else:
            if len(nums) != 11:
                continue
            cur = geo.dims(label)
            if cur is None:
                continue
            area = "total"
            sqkm, pop, male, female, trans, ratio, dens, urban, hh, p17, gr = nums
            for sid, v in (("area", sqkm), ("density", dens), ("urban_share", urban)):
                if v is not None:
                    out.append(Record(f"regional.census.{sid}", OBS_DATE, v, dict(cur)))
        if cur is None:
            continue
        d = {**cur, "locality": area}
        for sex, v in (("all", pop), ("male", male), ("female", female), ("transgender", trans)):
            if v is not None:
                out.append(Record("regional.census.population", OBS_DATE, v, {**d, "sex": sex}))
        for sid, v in (("sex_ratio", ratio), ("household_size", hh), ("population_2017", p17), ("growth_rate", gr)):
            if v is not None:
                out.append(Record(f"regional.census.{sid}", OBS_DATE, v, dict(d)))
    if not out:
        raise ValueError(f"no Table 1 rows parsed for {province}")
    return out


# Table 12: 13 numbered columns; data columns 2-13 = (total, rural, urban) x (all, male, female, trans).
_T12_COLS = [(a, s) for a in ("total", "rural", "urban") for s in ("all", "male", "female", "transgender")]
_T12_ROWS = {"literate %": "literacy_rate", "out of school children (5-16)": "out_of_school"}


def parse_table12_words(pages: list[list[dict]], province: str) -> list[Record]:
    """pages: pdfplumber words per page ({text, x0, x1, top}). Pure."""
    out: list[Record] = []
    geo, cur = _Geo(province), None
    for words in pages:
        # the column-number header: the line holding 1..13 in order
        lines: dict[int, list[dict]] = {}
        for w in words:
            lines.setdefault(round(w["top"]), []).append(w)
        centres = None
        for top in sorted(lines):
            nums = [w for w in sorted(lines[top], key=lambda w: w["x0"]) if re.fullmatch(r"\d{1,2}", w["text"])]
            if [w["text"] for w in nums][:13] == [str(i) for i in range(1, 14)]:
                centres = [(w["x0"] + w["x1"]) / 2 for w in nums[:13]]
                hdr_top = top
                break
        if not centres:
            continue
        edges = [(centres[i] + centres[i + 1]) / 2 for i in range(12)]
        for top in sorted(t for t in lines if t > hdr_top):
            ws = sorted(lines[top], key=lambda w: w["x0"])
            if not any(re.search(r"\d", w["text"]) for w in ws):  # a place heading, centred on the page
                heading = " ".join(w["text"] for w in ws).strip()
                if heading.isupper():
                    cur = geo.dims(heading)
                continue
            col: dict[int, str] = {}
            label_parts = []
            for w in ws:
                c = sum(1 for e in edges if (w["x0"] + w["x1"]) / 2 > e)  # 0-based column
                if c == 0:
                    label_parts.append(w["text"])
                else:
                    col[c] = col.get(c, "") + w["text"]
            label = " ".join(label_parts).strip()
            sid = _T12_ROWS.get(label.lower())
            if not sid or cur is None:
                continue
            for i, (area, sex) in enumerate(_T12_COLS, start=1):
                raw = col.get(i, "").replace(",", "")
                if not raw or raw == "-":
                    continue
                try:
                    v = float(raw)
                except ValueError:
                    continue
                out.append(Record(f"regional.census.{sid}", OBS_DATE, v, {**cur, "locality": area, "sex": sex}))
    if not out:
        raise ValueError(f"no Table 12 rows parsed for {province}")
    return out


class PbsCensus2023Job(IngestionJob):
    name = "pbs_census_2023"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        files = [FetchedFile(filename=f"census2023_list_{p}.pdf", content=self.http_get(url), when=OBS_DATE)
                 for p, url in DISTRICT_LISTS.items()]
        return files + [FetchedFile(filename=f"census2023_t{t}_{p}.pdf", content=self.http_get(url), when=OBS_DATE)
                        for (t, p), url in sorted(FILES.items())]

    def parse(self, f: FetchedFile) -> list[Record]:
        import pdfplumber

        kind, p = re.match(r"census2023_(list|t\d+)_(\w+)\.pdf", f.filename).groups()
        self._districts = getattr(self, "_districts", {})
        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            text = lambda: "\n".join(pg.extract_text() or "" for pg in pdf.pages)  # noqa: E731
            if kind == "list":  # fetched before the tables
                self._districts[p] = district_names(text(), PROVINCES[p])
                return []
            if kind == "t1":
                return parse_table1(text(), PROVINCES[p], self._districts.get(p))
            return parse_table12_words([pg.extract_words(x_tolerance=1) for pg in pdf.pages], PROVINCES[p])
