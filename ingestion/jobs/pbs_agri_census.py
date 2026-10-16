"""pbs_agri_census - Agricultural Census 2024: livestock, farms, tractors and tubewells by district.

Source: PBS Agricultural Census 2024 district-level tables (pbs.gov.pk → Agriculture Census),
https://www.pbs.gov.pk/wp-content/uploads/2020/07/:
    Table_6_District.xls   livestock population: cattle, buffaloes, sheep, goats, camels, horses,
                           mules, asses, yak/dzo/dzomo                          (Excel 97, Crystal Reports)
    Table_3_District.xls   number and area of private farms by farm size        (Excel 97)
    p3_prv_tbl_01.xlsx     tractors by type of ownership
    p3_prv_tbl_3.xlsx      tubewells and lift pumps by operating power
Rows run province → division → district ("BANNU DISTRICT"); a few areas carry no suffix
("MALAKAND PROTECTED AREA", "CHOLISTAN AREA") and are kept at district level. Divisions are
skipped. Census figures are sample-weighted estimates; we round them to whole numbers.

Series (dims `province`, `district` [absent on province rows] + the breakdown named below):
    regional.agri_census.livestock      `animal`
    regional.agri_census.farms          `farm_size` (acres; "all" for the total)   number of farms
    regional.agri_census.farm_area      `farm_size`                                acres
    regional.agri_census.cultivated_area `farm_size`                               acres
    regional.agri_census.tractors       `ownership` (all, individual, joint, cooperative)
    regional.agri_census.tubewells      `kind` (tubewell, lift pump), `power` (all, electric, solar,
                                        diesel, petrol, other)
Dated 2024-12-31. PBS blocks the VPS: run from a workstation; the census is fixed.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record

BASE = "https://www.pbs.gov.pk/wp-content/uploads/2020/07"
FILES = {"livestock": "Table_6_District.xls", "farms": "Table_3_District.xls",
         "tractors": "p3_prv_tbl_01.xlsx", "tubewells": "p3_prv_tbl_3.xlsx"}
OBS_DATE = date(2024, 12, 31)
SOURCE_URL = "https://www.pbs.gov.pk/agriculture-census/"
PROVINCES = {"KHYBER PAKHTUNKHWA": "Khyber Pakhtunkhwa", "PUNJAB": "Punjab", "SINDH": "Sindh",
             "BALOCHISTAN": "Balochistan", "ISLAMABAD CAPITAL TERRITORY": "Islamabad",
             "ISLAMABAD": "Islamabad", "PAKISTAN": None}
ANIMALS = ["cattle", "buffaloes", "sheep", "goats", "camels", "horses", "mules", "asses", "yak/dzo/dzomo"]

_C = dict(module="regional", frequency="census", source="PBS", source_url=SOURCE_URL, min_value=0, max_value=1e9)
_G = "for each province and district — Agricultural Census 2024 (sample-weighted estimates)"
CATALOG = [
    dict(id="regional.agri_census.livestock", name="Livestock population (Agricultural Census 2024)",
         unit="count", description=f"Number of animals by type (dimension `animal`), {_G}.", **_C),
    dict(id="regional.agri_census.farms", name="Number of private farms (Agricultural Census 2024)", unit="count",
         description=f"Number of private farms by farm size in acres (dimension `farm_size`), {_G}.", **_C),
    dict(id="regional.agri_census.farm_area", name="Private farm area (Agricultural Census 2024)", unit="acres",
         description=f"Area of private farms in acres by farm size (dimension `farm_size`), {_G}.", **_C),
    dict(id="regional.agri_census.cultivated_area", name="Cultivated area of private farms (Agricultural Census 2024)",
         unit="acres", description=f"Cultivated area in acres by farm size (dimension `farm_size`), {_G}.", **_C),
    dict(id="regional.agri_census.tractors", name="Tractors (Agricultural Census 2024)", unit="count",
         description=f"Tractors by type of ownership (dimension `ownership`), {_G}.", **_C),
    dict(id="regional.agri_census.tubewells", name="Tubewells and lift pumps (Agricultural Census 2024)",
         unit="count", description="Tubewells and lift pumps (dimension `kind`) by operating power (dimension "
                                   f"`power`), {_G}.", **_C),
]


class _Geo:
    def __init__(self):
        self.province = None

    def dims(self, label: str) -> dict | None:
        lab = re.sub(r"\s+", " ", label).strip().upper()
        if lab in PROVINCES:
            self.province = PROVINCES[lab]
            return {"province": self.province} if self.province else {}
        if lab.endswith(" DIVISION") or self.province is None:
            return None
        name = re.sub(r" DISTRICT$", "", lab)
        return {"province": self.province, "district": " ".join(w.capitalize() for w in name.split())}


def _rows_xls(content: bytes) -> list[list]:
    import xlrd

    sh = xlrd.open_workbook(file_contents=content).sheet_by_index(0)
    rows = []
    for i in range(sh.nrows):
        cells = [c for c in sh.row_values(i) if str(c).strip() != ""]
        if cells:
            rows.append(cells)
    return rows


def _rows_xlsx(content: bytes) -> list[list]:
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(content), data_only=True).active
    rows = []
    for r in ws.iter_rows(values_only=True):
        r = list(r)
        while r and r[0] is None:  # the label column is preceded by an empty one; keep later blanks in place
            r.pop(0)
        while r and r[-1] is None:
            r.pop()
        if r:
            rows.append(r)
    return rows


def _is_label(v) -> bool:
    return isinstance(v, str) and v.strip() and v.strip().upper() == v.strip() and not re.match(r"^\d", v.strip())


def _n(v) -> float | None:
    return round(float(v)) if isinstance(v, (int, float)) else None


def parse_by_unit(rows: list[list], sid: str, keys: list[dict]) -> list[Record]:
    """Rows of `LABEL v1 v2 …` → one record per value, dims from `keys` (one dict per column). Pure."""
    out, geo = [], _Geo()
    for r in rows:
        if not _is_label(r[0]) or len(r) < 2:  # trailing empty cells (e.g. no "other" lift pumps) are dropped
            continue
        d = geo.dims(r[0])
        if d is None:
            continue
        for extra, v in zip(keys, r[1:1 + len(keys)]):
            if (x := _n(v)) is not None:
                out.append(Record(sid, OBS_DATE, x, {**d, **extra}))
    if not out:
        raise ValueError(f"no rows parsed for {sid}")
    return out


def parse_farms(rows: list[list]) -> list[Record]:
    """Table 3: an area heading row, then 'PRIVATE-FARMS TOTAL' and one row per farm-size class:
    number, %, farm area, %, cultivated area, %, … Pure."""
    out, geo, cur = [], _Geo(), None
    for r in rows:
        if len(r) == 1 and _is_label(r[0]):
            cur = geo.dims(r[0])
            continue
        if cur is None or not isinstance(r[0], str) or len(r) < 6:
            continue
        size = "all" if "TOTAL" in r[0].upper() else r[0].strip().lower()
        for sid, v in (("farms", r[1]), ("farm_area", r[3]), ("cultivated_area", r[5])):
            if (x := _n(v)) is not None:
                out.append(Record(f"regional.agri_census.{sid}", OBS_DATE, x, {**cur, "farm_size": size}))
    if not out:
        raise ValueError("no farm rows parsed")
    return out


_POWER = ["all", "electric", "solar", "diesel", "petrol", "other"]
KEYS = {
    "livestock": [{"animal": a} for a in ANIMALS],
    "tractors": [{"ownership": o} for o in ("all", "individual", "joint", "cooperative")],
    # TOTAL TUBEWELLS/LIFT PUMPS, then tubewells by power (6), then lift pumps by power (6)
    "tubewells": [{"kind": "all", "power": "all"}] + [{"kind": "tubewell", "power": p} for p in _POWER]
                 + [{"kind": "lift pump", "power": p} for p in _POWER],
}


class PbsAgriCensusJob(IngestionJob):
    name = "pbs_agri_census"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        return [FetchedFile(filename=f"agri_census_{k}.{fn.rsplit('.', 1)[1]}", content=self.http_get(f"{BASE}/{fn}"),
                            when=OBS_DATE) for k, fn in FILES.items()]

    def parse(self, f: FetchedFile) -> list[Record]:
        kind = re.match(r"agri_census_(\w+)\.", f.filename).group(1)
        rows = _rows_xls(f.content) if f.filename.endswith(".xls") else _rows_xlsx(f.content)
        if kind == "farms":
            return parse_farms(rows)
        return parse_by_unit(rows, f"regional.agri_census.{kind}", KEYS[kind])
