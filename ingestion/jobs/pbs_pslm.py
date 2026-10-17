"""pbs_pslm - PSLM district survey: education, connectivity, health and living conditions by district.

Source: PBS Pakistan Social & Living Standards Measurement survey, district-level round 2019-20
(pbs.gov.pk/pslm-3): one PDF per table, every province and district with urban / rural / total and
male / female / total columns. Several tables repeat the 2014-15 district round's total in a last
column ("TOTAL 14-15"), which gives a second point for those indicators (dated 2015-06-30; 2019-20
is dated 2020-06-30).

The tables are typeset as positioned text. `extract_table` clusters each page's number columns by
their right edge, merges a row's label and numbers when the PDF puts them a point apart, and hands
back (label, {column: value}) rows; each table's `SPECS` entry says what every column means.
District spellings ("Bahawalnager", "D.G.khan", "Jaccobabad") go through `geo_pk.canon_district`.
PBS blocks the VPS: workstation job (`dq_fetch_parse.py job pbs_pslm`).
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion import geo_pk
from ingestion.framework import FetchedFile, IngestionJob, Record

PAGE_URL = "https://www.pbs.gov.pk/pslm-3/"
D20, D15 = date(2020, 6, 30), date(2015, 6, 30)
_NUM = re.compile(r"^-?\d*\.?\d+%?$")

_SL = [(loc, sex) for loc in ("urban", "rural", "total") for sex in ("male", "female", "all")]
_LEVEL = {"2.3a": "primary", "2.6a": "primary", "2.10a": "middle", "2.11a": "middle", "2.12a": "matric", "2.13a": "matric"}

# table number → (series id, columns). A column is a dims dict ("loc" = locality), None = skip,
# or ("2015", dims) = the 2014-15 comparison column. Row-level Urban/Rural rows set locality.
_SEXLOC = [dict(locality=lo, sex=sx) for lo, sx in _SL]
_RANKED = _SEXLOC + [None, ("2015", dict(locality="total", sex="all"))]
SPECS: dict[str, tuple[str, list]] = {
    "2.1": ("regional.pslm.ever_attended_school", _SEXLOC + [None]),
    "2.3a": ("regional.pslm.enrolment_gross", _RANKED),
    "2.6a": ("regional.pslm.enrolment_net", _RANKED),
    "2.10a": ("regional.pslm.enrolment_gross", _RANKED),
    "2.11a": ("regional.pslm.enrolment_net", _RANKED),
    "2.12a": ("regional.pslm.enrolment_gross", _RANKED),
    "2.13a": ("regional.pslm.enrolment_net", _RANKED),
    "2.14b": ("regional.pslm.adult_literacy", _RANKED),
    "2.15": ("regional.pslm.out_of_school", _SEXLOC + [None]),
    "3.2": ("regional.pslm.mobile_ownership", _SEXLOC),
    "3.3": ("regional.pslm.internet_use", _SEXLOC),
    "4.6": ("regional.pslm.child_diarrhoea", _RANKED),
    "5.2": ("regional.pslm.disability", _SEXLOC),
    "3.1": ("regional.pslm.household_ict", [dict(item=i) for i in ("computer, laptop or tablet", "mobile phone",
                                                                   "smartphone")]),
    "4.5": ("regional.pslm.immunisation", [dict(antigen=a) for a in ("BCG", "Penta 1", "Penta 2", "Penta 3",
                                                                      "Pneumococcal 1", "Pneumococcal 2",
                                                                      "Pneumococcal 3", "Polio 1", "Polio 2",
                                                                      "Polio 3", "Measles")]),
    "7.1": ("regional.pslm.drinking_water", [dict(source=s) for s in ("tap water", "hand pump", "motor pump",
                                                                       "dug well", "tanker, truck or water bearer",
                                                                       "filtration plant", "other")]
            + [None, None, ("2015", dict(source="tap water"))]),
    "7.2": ("regional.pslm.toilet", [dict(locality=lo, toilet=t) for t in ("flush", "non-flush", "none")
                                     for lo in ("urban", "rural", "total")]
            + [None, ("2015", dict(locality="total", toilet="flush"))]),
    "7.3": ("regional.pslm.waste_disposal", [dict(method=m) for m in ("municipal or private collection",
                                                                       "public bin", "road or street",
                                                                       "open space", "other")] + [None]),
    "7.8": ("regional.pslm.lighting_source", [dict(source=s) for s in ("electricity", "solar panel", "gas",
                                                                        "other")] + [None]),
    "7.9": ("regional.pslm.cooking_fuel", [dict(fuel=f) for f in ("gas", "wood or sticks", "oil", "other")] + [None]),
    "8.3": ("regional.pslm.service_satisfaction", [dict(facility=f) for f in (
        "basic health unit", "family planning", "primary school", "middle school", "high school", "veterinary",
        "agriculture extension", "police")]),
    "9.1": ("regional.pslm.food_insecurity", [dict(severity="moderate or severe"), None, dict(severity="severe"),
                                              None]),
    "6.1": ("regional.pslm.migration", [dict(status="native"), dict(status="migrated"),
                                        dict(status="migrated within province"),
                                        dict(status="migrated from another province")]),
}

# gross enrolment counts over-age pupils too: small urban samples print up to 267 (Karak, matric)
_C = dict(module="regional", frequency="survey", source="PBS", source_url=PAGE_URL, unit="percent", min_value=0,
          max_value=400)
_GEO = ("for Pakistan, each province and every district, from PBS's PSLM district survey 2019-20 "
        "(with 2014-15 district figures where PBS repeats them)")
_SERIES = {
    "regional.pslm.adult_literacy": ("Adult literacy rate (age 15+), by district",
                                     "Share of people aged 15 and over who can read and write", {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.ever_attended_school": ("Population that has ever attended school, by district",
                                           "Share of the population aged 10 and over that has ever attended school",
                                           {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.enrolment_gross": ("Gross enrolment ratio, by district",
                                      "Gross enrolment ratio at the primary (excluding katchi), middle and matric "
                                      "levels (dimension level)", {"level": ["primary"], "sex": ["all"], "locality": ["total"]}),
    "regional.pslm.enrolment_net": ("Net enrolment rate, by district",
                                    "Net enrolment rate at the primary, middle and matric levels (dimension level)",
                                    {"level": ["primary"], "sex": ["all"], "locality": ["total"]}),
    "regional.pslm.out_of_school": ("Out-of-school children (age 5-16), by district",
                                    "Share of children aged 5-16 who are not in school", {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.mobile_ownership": ("Mobile phone ownership, by district",
                                       "Share of individuals who own a mobile phone", {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.internet_use": ("Internet use, by district", "Share of individuals who use the internet",
                                   {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.household_ict": ("Households with a computer, mobile or smartphone, by district",
                                    "Share of households that have a computer/laptop/tablet, a mobile phone or a "
                                    "smartphone (dimension item)", {"item": ["smartphone"], "locality": ["total"]}),
    "regional.pslm.child_diarrhoea": ("Children under 5 with diarrhoea, by district",
                                      "Share of children under 5 who had diarrhoea in the past 30 days",
                                      {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.disability": ("People with a functional limitation, by district",
                                 "Share of people with a functional limitation (disability)",
                                 {"sex": ["all"], "locality": ["total"]}),
    "regional.pslm.immunisation": ("Child immunisation coverage, by district",
                                   "Share of children aged 12-23 months immunised, by antigen (record and recall)",
                                   {"antigen": ["Measles"], "locality": ["total"]}),
    "regional.pslm.drinking_water": ("Households by source of drinking water, by district",
                                     "Share of households by main source of drinking water (dimension source)",
                                     {"source": ["tap water"], "locality": ["total"]}),
    "regional.pslm.toilet": ("Households by type of toilet, by district",
                             "Share of households with a flush toilet, a non-flush toilet or none (dimension toilet)",
                             {"toilet": ["flush"], "locality": ["total"]}),
    "regional.pslm.waste_disposal": ("Households by solid-waste disposal, by district",
                                     "Share of households by how solid waste is disposed of (dimension method)",
                                     {"method": ["municipal or private collection"], "locality": ["total"]}),
    "regional.pslm.lighting_source": ("Households by source of lighting, by district",
                                      "Share of households by main source of lighting (dimension source)",
                                      {"source": ["electricity"], "locality": ["total"]}),
    "regional.pslm.cooking_fuel": ("Households by cooking fuel, by district",
                                   "Share of households by main cooking fuel (dimension fuel)",
                                   {"fuel": ["gas"], "locality": ["total"]}),
    "regional.pslm.service_satisfaction": ("Satisfaction with public services, by district",
                                           "Share of households that use and are satisfied with each public service "
                                           "(dimension facility)", {"facility": ["police"], "locality": ["total"]}),
    "regional.pslm.food_insecurity": ("Food insecurity, by district",
                                      "Share of households facing moderate-or-severe and severe food insecurity "
                                      "(Food Insecurity Experience Scale)", {"severity": ["moderate or severe"]}),
    "regional.pslm.migration": ("Migration status, by district",
                                "Share of people living in their district of birth or migrated from elsewhere in the "
                                "province or another province (dimension status)", {"status": ["migrated"]}),
}
CATALOG = [dict(id=sid, name=name, description=f"{desc} {_GEO}.", dimensions=dims, **_C)
           for sid, (name, desc, dims) in _SERIES.items()]


# ---- positioned-text table extraction --------------------------------------------------------------

def _lines(words, tol: float = 3.0):
    out, cur, y = [], [], None
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if y is not None and w[1] - y > tol:
            out.append(sorted(cur, key=lambda w: w[0]))
            cur = []
        if not cur:
            y = w[1]
        cur.append(w)
    if cur:
        out.append(sorted(cur, key=lambda w: w[0]))
    return out


def extract_table(words, label_max_x: float = 150.0) -> list[tuple[str, dict[int, float]]]:
    """words: (x0, top, x1, bottom, text) for one page → [(row label, {column index: value})]. Pure."""
    ls = _lines(words)
    data = []
    for line in ls:
        nums = [w for w in line if _NUM.match(w[4]) and w[0] > 110]
        label = " ".join(w[4] for w in line if w not in nums and w[0] < label_max_x).strip()
        if nums or (label and not line[0][4].upper().startswith(("TABLE", "PROVINCE"))):
            data.append([line[0][1], label, nums])
    rights = sorted(w[2] for _, _, nums in data for w in nums)
    cols: list[list[float]] = []
    for r in rights:
        if cols and r - cols[-1][-1] <= 10:
            cols[-1].append(r)
        else:
            cols.append([r])
    centers = [sum(c) / len(c) for c in cols if len(c) >= 2]
    rows: list[tuple[str, dict[int, float]]] = []
    for i, (y, label, nums) in enumerate(data):
        if not nums:
            continue
        vals: dict[int, float] = {}
        for w in nums:
            k = min(range(len(centers)), key=lambda k: abs(centers[k] - w[2]))
            if abs(centers[k] - w[2]) <= 12:
                vals[k] = float(w[4].rstrip("%"))
        if not label:  # the label sits a point off its numbers: take the nearest label-only line
            near = [(abs(yy - y), lab) for yy, lab, nn in data if lab and not nn and abs(yy - y) < 7]
            if near:
                label = min(near)[1]
        if rows and not label and set(vals).isdisjoint(rows[-1][1]):
            rows[-1][1].update(vals)
            continue
        if rows and label and rows[-1][0] == "" and set(vals).isdisjoint(rows[-1][1]):
            vals.update(rows[-1][1])
            rows[-1] = (label, vals)
            continue
        rows.append((label, vals))
    return rows


def table_number(name: str) -> str | None:
    m = re.match(r"Table-(\d+\.\d+)[-\s]?(a|b)?\b", name, re.I)
    if not m:
        return None
    return m.group(1) + (m.group(2) or "").lower()


def to_records(tab: str, rows: list[tuple[str, dict[int, float]]]) -> list[Record]:
    sid, cols = SPECS[tab]
    out: list[Record] = []
    place: dict | None = None
    province: str | None = None
    for label, vals in rows:
        lab = re.sub(r"\s+", " ", label).strip()
        low = lab.lower()
        locality = None
        if low in ("urban", "rural", "u", "r"):
            if place is None:
                continue
            locality = "urban" if low.startswith("u") else "rural"
        elif low == "pakistan":
            place, province = {}, None
        elif low in ("islamabad", "ict"):  # listed among Punjab's districts: a place, not a province switch
            place = {"province": "Islamabad", "district": "Islamabad"}
        elif geo_pk.canon_province(lab):
            province = geo_pk.canon_province(lab)
            place = {"province": province}
        else:
            hit = geo_pk.canon_district(lab, province) if lab else None
            if hit is None:
                place = None  # an unrecognised label (sub-heading, footnote): skip its rows
                continue
            place = {"province": hit[0], "district": hit[1]}
        if place is None:
            continue
        for k, v in vals.items():
            spec = cols[k] if k < len(cols) else None
            if spec is None:
                continue
            when = D20
            if isinstance(spec, tuple):
                when, spec = D15, spec[1]
            dims = {**place, **spec}
            if "locality" not in spec:
                dims["locality"] = locality or "total"
            elif locality:
                continue  # a sex x locality table has no Urban/Rural rows; ignore stray ones
            if tab in _LEVEL:
                dims["level"] = _LEVEL[tab]
            out.append(Record(sid, when, v, dims))
    return out


def parse_pdf(content: bytes, name: str) -> list[Record]:
    tab = table_number(name)
    if tab not in SPECS:
        return []
    try:
        import pymupdf  # workstation backfills; not in the image

        pages = [[(w[0], w[1], w[2], w[3], w[4]) for w in p.get_text("words")]
                 for p in pymupdf.open(stream=content, filetype="pdf")]
    except ImportError:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [[(w["x0"], w["top"], w["x1"], w["bottom"], w["text"]) for w in p.extract_words()]
                     for p in pdf.pages]
    out: list[Record] = []
    for words in pages:
        out += to_records(tab, extract_table(words))
    return out


class PbsPslmJob(IngestionJob):
    name = "pbs_pslm"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        html = self.http_get(PAGE_URL).decode("utf-8", "replace")
        urls = sorted({u for u in re.findall(r'href="([^"]+/Table-[^"]+\.pdf)"', html)
                       if table_number(u.rsplit("/", 1)[-1]) in SPECS})
        return [FetchedFile(u.rsplit("/", 1)[-1], self.http_get(u), date.today()) for u in urls]

    def parse(self, f: FetchedFile) -> list[Record]:
        return parse_pdf(f.content, f.filename)
