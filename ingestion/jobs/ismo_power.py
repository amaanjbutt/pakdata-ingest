"""ismo_power - ISMO power-market data: marginal price, demand, consumption by DISCO, generation.

Source: the Independent System & Market Operator's website (ismo.gov.pk). Its dashboard pulls
everything from a JSON API (`/api/v1/mo-data`, `/api/v1/documents`) that sends the same public
bearer token the site's own JavaScript bundle carries; we read that token from the live bundle
at run time rather than storing it. Files used:

- "Hourly Marginal Price Data": one workbook, Date | Hour | Marginal Price (Rs/kWh), July 2024 →.
  → `energy.power.marginal_price.hourly` (dim `hour`), `.daily` (mean), `.daily_max`, `.daily_min`,
    `.monthly` (mean of the hours).
- Monthly market-statistics workbooks ("System at a Glance", "Fuel Mix", "DISCO's Consumption",
  ... — every dataset links the same workbook) for an *average day* of the month (the DISCO sheet's
  column is headed "Avg"). Early uploads (Feb–Aug 2023) are single-day snapshots headed
  "Grand Total" and are skipped. ISMO re-uploads stale sheets under new month names (June and July
  2024 are byte-identical; the DISCO sheet repeats Jun→Sep 2025), so a sheet whose figures appear
  under two different months is dropped for all of them, and from July 2024 a workbook is used
  only if its hourly marginal-price profile matches the one rebuilt from the hourly price file.
  → `energy.power.disco_consumption` (GWh/day, dim `disco`), `energy.power.generation_mix`
    (average MW, dim `source`), `energy.power.demand` (average MW, dim `grid`).
- "Hourly Generation Data (FY 2025)": metered hourly kWh per station, one sheet per fuel.
  → `energy.generation.plant_daily` (MWh/day, dims `plant`, `fuel`).
- "Hourly System Demand FY 2025": metered hourly demand excluding K-Electric (MW).
  → `energy.power.demand_daily` (dim `stat`: average / peak).

ismo.gov.pk 403s the VPS, so this is a workstation/GitHub job (`dq_fetch_parse.py job ismo_power`).
"""
from __future__ import annotations

import calendar
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime

from ingestion.framework import FetchedFile, IngestionJob, Record

SITE = "https://www.ismo.gov.pk"
API = f"{SITE}/api/v1"
SOURCE_URL = f"{SITE}/dashboard"

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
_MONTH_NAME = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?[\s\-']*(\d{4}|\d{2})\b", re.I)
_DOC_CATS = {"hourly-generation": "plants", "hourly-system-demand": "demand"}
GRID_NTDC = "National grid (excl. K-Electric)"
GRID_KE = "K-Electric"
_SOURCES = {"baggasse": "Bagasse", "bagasse": "Bagasse", "coal": "Coal", "hydel": "Hydel", "hydro": "Hydel",
            "nuclear": "Nuclear", "solar": "Solar", "thermal": "Thermal (gas, RLNG, oil)", "wind": "Wind"}
_FUELS = {"Wind": "Wind", "Solar": "Solar", "RLNG": "RLNG", "Dom Coal": "Local coal", "Imp Coal": "Imported coal",
          "Nuclear": "Nuclear", "Hydel": "Hydel", "Gas": "Gas", "GENCO": "Gas (GENCO)", "RFO": "Furnace oil",
          "Baggasse": "Bagasse"}

_LEAD = "ISMO, the operator of Pakistan's national grid and wholesale electricity market."
CATALOG = [
    dict(id="energy.power.marginal_price.hourly", module="energy", name="Electricity marginal price, hourly",
         unit="pkr_per_kwh", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200,
         dimensions={"hour": ["18"]},
         description="The system marginal price of electricity in Pakistan for every hour of the day (dimension "
                     "hour, 0-23): the cost of the most expensive plant running to meet demand, Rs per kWh. "
                     f"July 2024 onward. Source: {_LEAD}"),
    dict(id="energy.power.marginal_price.daily", module="energy", name="Electricity marginal price, daily average",
         unit="pkr_per_kwh", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200,
         description="The daily average of Pakistan's hourly system marginal price of electricity, Rs per kWh. "
                     f"July 2024 onward. Source: {_LEAD}"),
    dict(id="energy.power.marginal_price.daily_max", module="energy", name="Electricity marginal price, daily high",
         unit="pkr_per_kwh", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200,
         description="The highest hourly system marginal price of electricity in Pakistan each day, Rs per kWh. "
                     f"July 2024 onward. Source: {_LEAD}"),
    dict(id="energy.power.marginal_price.daily_min", module="energy", name="Electricity marginal price, daily low",
         unit="pkr_per_kwh", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200,
         description="The lowest hourly system marginal price of electricity in Pakistan each day, Rs per kWh. "
                     f"July 2024 onward. Source: {_LEAD}"),
    dict(id="energy.power.marginal_price.monthly", module="energy", name="Electricity marginal price, monthly average",
         unit="pkr_per_kwh", frequency="monthly", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200,
         description="The monthly average of Pakistan's hourly system marginal price of electricity, Rs per kWh. "
                     f"July 2024 onward. Source: {_LEAD}"),
    dict(id="energy.power.disco_consumption", module="energy", name="Electricity drawn by each distribution company",
         unit="gwh", frequency="monthly", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=1000,
         dimensions={"disco": ["LESCO"]},
         description="Average daily electricity drawn from the grid by each distribution company (dimension disco: "
                     "LESCO, MEPCO, FESCO, IESCO, GEPCO, PESCO, HESCO, SEPCO, QESCO and K-Electric), GWh per day, "
                     f"for each month since September 2023. Source: {_LEAD}"),
    dict(id="energy.power.generation_mix", module="energy", name="Grid electricity generation by fuel",
         unit="mw", frequency="monthly", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=40000,
         dimensions={"source": ["Hydel"]},
         description="Average generation feeding Pakistan's national grid by fuel (dimension source: hydel, coal, "
                     "nuclear, thermal, wind, solar, bagasse), MW averaged over the month, since September 2023. "
                     f"Source: {_LEAD}"),
    dict(id="energy.power.demand", module="energy", name="Electricity demand on the grid",
         unit="mw", frequency="monthly", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=50000,
         dimensions={"grid": [GRID_NTDC]},
         description="Average electricity demand, MW, for each month since September 2023: the national grid "
                     f"excluding K-Electric, and K-Electric's draw (dimension grid). Source: {_LEAD}"),
    dict(id="energy.power.demand_daily", module="energy", name="Electricity demand on the national grid, daily",
         unit="mw", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=50000,
         dimensions={"stat": ["average"]},
         description="Metered electricity demand on Pakistan's national grid excluding K-Electric, each day of "
                     "FY2024-25: the daily average and the daily peak hour (dimension stat), MW. "
                     f"Source: {_LEAD}"),
    dict(id="energy.generation.plant_daily", module="energy", name="Daily electricity generation by power plant",
         unit="mwh", frequency="daily", source="ISMO", source_url=SOURCE_URL, min_value=0, max_value=200000,
         description="Metered electricity generation of every grid-connected power plant each day of FY2024-25 "
                     "(dimensions plant and fuel), MWh. Source: " + _LEAD),
]


def _ym_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def month_of(name: str) -> date | None:
    """'Market Statistics Nov 23' / 'Fuel Mix Aug 2026' → month end. None for day-dated names. Pure."""
    if re.search(r"\d{1,2}-\d{1,2}-\d{2,4}", name):
        return None
    m = _MONTH_NAME.search(name)
    if not m:
        return None
    y = int(m.group(2))
    y = y + 2000 if y < 100 else y
    return _ym_end(y, _MONTHS[m.group(1).lower()[:3]])


def _num(v) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _day(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _sheets(content: bytes) -> dict[str, list[tuple]]:
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    return {ws.title.strip(): [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
            for ws in wb.worksheets}


def parse_marginal_price(rows: list[tuple]) -> list[Record]:
    """Date | Hour | Rs/kWh rows → hourly, daily mean/max/min and monthly mean records. Pure."""
    by_day: dict[date, dict[int, float]] = defaultdict(dict)
    for r in rows:
        d, h, v = (_day(r[0]), _num(r[1]), _num(r[2])) if len(r) >= 3 else (None, None, None)
        if d is None or h is None or v is None or not 0 <= h <= 23:
            continue
        by_day[d][int(h)] = v
    out: list[Record] = []
    by_month: dict[date, list[float]] = defaultdict(list)
    for d in sorted(by_day):
        hours = by_day[d]
        for h, v in sorted(hours.items()):
            out.append(Record("energy.power.marginal_price.hourly", d, round(v, 4), {"hour": str(h)}))
        vals = list(hours.values())
        if len(vals) >= 20:  # a part day would bias the daily figures
            out.append(Record("energy.power.marginal_price.daily", d, round(sum(vals) / len(vals), 4), {}))
            out.append(Record("energy.power.marginal_price.daily_max", d, round(max(vals), 4), {}))
            out.append(Record("energy.power.marginal_price.daily_min", d, round(min(vals), 4), {}))
        by_month[_ym_end(d.year, d.month)] += vals
    for m, vals in sorted(by_month.items()):
        days = len({d for d in by_day if _ym_end(d.year, d.month) == m})
        if days >= calendar.monthrange(m.year, m.month)[1] - 1:
            out.append(Record("energy.power.marginal_price.monthly", m, round(sum(vals) / len(vals), 4), {}))
    return out


def parse_month_workbook(sheets: dict[str, list[tuple]], month: date) -> list[Record]:
    """An average-day market-statistics workbook → monthly records (each sheet on its own). Pure."""
    disco = sheets.get("DISCO Consumption") or []
    if disco and str(disco[0][1]).strip().lower() != "avg":
        disco = []  # "Grand Total" = a single-day snapshot; no header = a malformed upload
    out: list[Record] = []
    for r in disco[1:]:
        name, gwh = (str(r[0]).strip() if r[0] else ""), _num(r[2]) if len(r) > 2 else None
        if name and gwh is not None and re.match(r"^[A-Z][A-Za-z\-]+$", name):
            out.append(Record("energy.power.disco_consumption", month, round(gwh, 3), {"disco": name}))
    mix = sheets.get("Generation Mix") or []
    if mix:
        head = [str(h or "").strip().lower() for h in mix[0]]
        try:
            col = head.index("grand total (mwh)")  # = the mean of the 24 hourly MW columns
        except ValueError:
            col = None
        for r in mix[1:]:
            src = _SOURCES.get(str(r[0] or "").strip().lower())
            if src and col is not None and _num(r[col]) is not None:
                out.append(Record("energy.power.generation_mix", month, round(_num(r[col]), 2), {"source": src}))
    dem = [(_num(r[0]), _num(r[1])) for r in (sheets.get("Marginal Cost - Demand") or [])[1:] if len(r) > 1]
    dem = [v for h, v in dem if h is not None and v is not None]
    if len(dem) == 24 and sum(dem) / 24 >= 4000:  # Feb-2026's upload carries a broken demand column
        out.append(Record("energy.power.demand", month, round(sum(dem) / 24, 1), {"grid": GRID_NTDC}))
    ke = [_num(r[1]) for r in (sheets.get("KE-Demand") or [])[1:] if len(r) > 1 and _num(r[1]) is not None]
    if len(ke) == 24 and len(dem) == 24 and sum(dem) / 24 >= 4000:
        out.append(Record("energy.power.demand", month, round(sum(ke) / 24, 1), {"grid": GRID_KE}))
    return out


def _profile(sheets: dict[str, list[tuple]]) -> list[float] | None:
    rows = [(_num(r[0]), _num(r[2])) for r in (sheets.get("Marginal Cost - Demand") or [])[1:] if len(r) > 2]
    vals = [v for h, v in rows if h is not None and v is not None]
    return vals if len(vals) == 24 else None


def _block_sigs(recs: list[Record]) -> dict[str, tuple]:
    sig: dict[str, list] = defaultdict(list)
    for r in recs:
        sig[r.series_id].append((json.dumps(r.dims, sort_keys=True), r.value))
    return {k: tuple(sorted(v)) for k, v in sig.items()}


def parse_bundle(files: dict[str, bytes]) -> list[Record]:
    """The hourly price workbook + every monthly workbook (named month_YYYY-MM_<upload>.xlsx) →
    records, with the cross-checks described in the module docstring. Pure."""
    out: list[Record] = []
    price_profile: dict[date, list[float]] = {}
    if "price.xlsx" in files:
        price = parse_marginal_price(next(iter(_sheets(files["price.xlsx"]).values()), []))
        out += price
        acc: dict[date, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        for r in price:
            if r.series_id == "energy.power.marginal_price.hourly":
                acc[_ym_end(r.obs_date.year, r.obs_date.month)][int(r.dims["hour"])].append(r.value)
        price_profile = {m: [sum(h[i]) / len(h[i]) for i in range(24)] for m, h in acc.items() if len(h) == 24}
    chosen: dict[date, tuple[str, list[Record]]] = {}
    for name in sorted(n for n in files if n.startswith("month_")):  # upload date sorts within a month
        y, mo = map(int, name[6:13].split("-"))
        m = _ym_end(y, mo)
        try:
            sheets = _sheets(files[name])
        except Exception:  # noqa: BLE001 — ISMO links some uploads to an HTML error page
            continue
        recs = parse_month_workbook(sheets, m)
        if not recs:
            continue
        ref, prof = price_profile.get(m), _profile(sheets)
        if ref and (prof is None or sum(abs(a - b) for a, b in zip(ref, prof)) / 24 > 0.5):
            continue  # mislabelled month
        chosen[m] = (name, recs)
    seen: dict[tuple[str, tuple], set[date]] = defaultdict(set)
    for m, (_, recs) in chosen.items():
        for sid, sig in _block_sigs(recs).items():
            seen[(sid, sig)].add(m)
    for m, (_, recs) in sorted(chosen.items()):
        sigs = _block_sigs(recs)
        out += [r for r in recs if len(seen[(r.series_id, sigs[r.series_id])]) == 1]
    return out


def plant_name(station: str) -> str:
    """'Coal : Engro Thar (660MW)' → 'Engro Thar (660MW)'; 'Wind: ACT 2 Wind' → 'ACT 2 Wind'. Pure."""
    return re.sub(r"^(wind|solar|coal|nuclear)\s*:\s*", "", station.strip(), flags=re.I).strip()


def parse_plants(sheets: dict[str, list[tuple]]) -> list[Record]:
    """Hourly kWh per station (one sheet per fuel) → daily MWh per plant. Pure."""
    tot: dict[tuple[date, str, str], float] = defaultdict(float)
    for title, rows in sheets.items():
        fuel = _FUELS.get(title)
        if not fuel:
            continue
        for r in rows[1:]:
            if len(r) < 4 or not r[3]:
                continue
            d, kwh = _day(r[0]), _num(r[2])
            if d is None or kwh is None:
                continue
            tot[(d, plant_name(str(r[3])), fuel)] += kwh
    return [Record("energy.generation.plant_daily", d, round(kwh / 1000, 3), {"plant": p, "fuel": f})
            for (d, p, f), kwh in sorted(tot.items())]


def parse_demand(rows: list[tuple]) -> list[Record]:
    """Date | Hour | MW → daily average and peak. Pure."""
    by_day: dict[date, list[float]] = defaultdict(list)
    for r in rows:
        if len(r) >= 3 and _day(r[0]) and _num(r[2]) is not None:
            by_day[_day(r[0])].append(_num(r[2]))
    out: list[Record] = []
    for d, v in sorted(by_day.items()):
        if len(v) >= 20:
            out.append(Record("energy.power.demand_daily", d, round(sum(v) / len(v), 1), {"stat": "average"}))
            out.append(Record("energy.power.demand_daily", d, round(max(v), 1), {"stat": "peak"}))
    return out


def _token(bundle_js: str) -> str:
    m = re.search(r'Authorization:"Bearer ([A-Za-z0-9]+)"', bundle_js)
    if not m:
        raise ValueError("ISMO bundle no longer carries a bearer token")
    return m.group(1)


class IsmoPowerJob(IngestionJob):
    name = "ismo_power"
    source = "ISMO"

    def _get(self, url: str, headers: dict | None = None) -> bytes:
        from ingestion import http_client

        return http_client.get(url.replace(" ", "%20"), timeout=180, headers=headers).content

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        home = self._get(SITE + "/").decode("utf-8", "replace")
        js = re.search(r'src="(/static/js/main\.[0-9a-f]+\.js)"', home)
        if not js:
            raise ValueError("ISMO home page no longer links its main bundle")
        auth = {"Authorization": "Bearer " + _token(self._get(SITE + js.group(1)).decode("utf-8", "replace")),
                "Accept": "application/json"}
        today = date.today()
        out: list[FetchedFile] = []
        bundle: dict[str, bytes] = {}
        for ds in json.loads(self._get(f"{API}/mo-data", auth)):
            files = ds.get("files") or []
            if "marginal price" in (ds.get("title") or "").lower() and "demand" not in (ds.get("title") or "").lower():
                if files:
                    bundle["price.xlsx"] = self._get(max(files, key=lambda f: f.get("date") or "")["file_path"])
                continue
            for f in files:
                m = month_of(f.get("name") or "")
                if m and (backfill or (today - m).days < 150):
                    key = f"month_{m:%Y-%m}_{(f.get('date') or '')[:10]}_{f.get('id')}.xlsx"
                    try:
                        bundle[key] = self._get(f["file_path"])
                    except Exception:  # noqa: BLE001 — ISMO links some uploads to missing files
                        continue
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for k, v in bundle.items():
                z.writestr(k, v)
        out.append(FetchedFile("ismo_bundle.zip", buf.getvalue(), today))
        if backfill:
            docs = json.loads(self._get(f"{API}/documents", auth)).get("data") or []
            for d in docs:
                kind = _DOC_CATS.get(d.get("category") or "")
                for url in (d.get("files") or [])[:1]:
                    if kind and url.lower().endswith(".xlsx"):
                        try:
                            out.append(FetchedFile(f"ismo_{kind}.xlsx", self._get(url), today))
                        except Exception:  # noqa: BLE001
                            continue
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        if f.filename == "ismo_bundle.zip":
            with zipfile.ZipFile(io.BytesIO(f.content)) as z:
                return parse_bundle({n: z.read(n) for n in z.namelist()})
        sheets = _sheets(f.content)
        first = next(iter(sheets.values()), [])
        if f.filename == "ismo_plants.xlsx":
            return parse_plants(sheets)
        if f.filename == "ismo_demand.xlsx":
            return parse_demand(first)
        return []
