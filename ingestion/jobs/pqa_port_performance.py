"""pqa_port_performance - Port Qasim's cargo by terminal × commodity × direction, ships and container TEUs,
every fiscal year from FY2014-15.

Source: Port Qasim Authority, "Port Performance Data of Last 5 years — Import/Export statistics at Port Qasim
Authority (000 Tonnes)", https://www.pqa.gov.pk/en/port-operations/port-performance — one HTML table, five
fiscal years (July–June) as columns, rewritten in place each year (the newest year provisional). Rows go
terminal by terminal ("MPT- MULTI-PURPOSE TERMINAL", "FOTCO- …", "EEPTL", "PIBT- …", "QICT - CONTAINER
TERMINAL"), each with its commodities under IMPORT / EXPORT (or DRY / LIQUID IMPORTS / EXPORTS) headings,
a sub-total and its number of ships; then PQA's totals (dry, liquid, imports, exports), the container
terminal's TEUs and the weight of its containers, the grand total and the total number of ships.

Earlier fiscal years come from the Internet Archive's captures of the same page (43 distinct copies, June
2019 →): `--backfill` reads them all and takes each year from the newest copy that prints it, so one value
is written per point. Every copy is also a dated release — `scripts/vintages_pqa.py` turns them into
revision vintages (first print and each restatement of a year).

Checks: each terminal's commodities must add up to its sub-total (MPT: its sections to "TOTAL MPT"); a
terminal that doesn't is logged and kept as published (PQA's own port totals don't always add up either —
"dry + liquid" isn't always the printed total; we store what is printed). Blank cells write nothing.

Series (module external, annual, dated 30 June of the fiscal year's end; 000 t and 000 TEU become t / TEU):
    ports.qasim.cargo_annual   t — level total (grand total, containers included) | cargo_type (cargo: dry,
                               liquid, containers, dry_and_liquid — by direction too) | terminal | commodity
    ports.qasim.ships_annual   ships handled — level total | terminal
    ports.qasim.teu_annual     container TEUs — direction
pqa.gov.pk answers the VPS; web.archive.org too.
"""
from __future__ import annotations

import html as H
import logging
import re
import time
from collections import defaultdict
from datetime import date

from ingestion import ports_pk
from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

PAGE_URL = "https://www.pqa.gov.pk/en/port-operations/port-performance"
CDX_URL = ("https://web.archive.org/cdx/search/cdx?url=pqa.gov.pk/en/port-operations/port-performance"
           "&output=json&fl=timestamp,statuscode,digest&filter=statuscode:200&collapse=digest")
WAYBACK = "https://web.archive.org/web/{}id_/" + PAGE_URL
PAUSE_S = 2.0
_FY = re.compile(r"^(\d{4})\s*-\s*(\d{2,4})$")
_SECTION = re.compile(r"^(?:(DRY|LIQUID)\s+)?(IMPORTS?|EXPORTS?)$", re.I)

_C = dict(module="external", frequency="annual", source="PQA", source_url=PAGE_URL, min_value=0)
_DIR = ["all", "import", "export"]
_FYS = "each fiscal year (July–June, dated 30 June) since FY2014-15, from Port Qasim Authority's port performance data"
CATALOG = [
    dict(id="ports.qasim.cargo_annual", name="Port Qasim cargo handled (fiscal year)", unit="metric ton",
         max_value=2e8, dimensions={"level": ["total", "cargo_type", "terminal", "commodity"], "direction": _DIR,
                                    "cargo": ["dry", "liquid", "containers", "dry_and_liquid"]},
         description=f"Tonnes of cargo handled at Port Qasim, {_FYS}: the grand total with containers (level "
                     f"total), by cargo type (level cargo_type, dimension cargo: dry, liquid, containers — their "
                     f"weight —, dry_and_liquid with direction import / export), by terminal (level terminal, "
                     f"dimension terminal) and by terminal, commodity and direction (level commodity). The latest "
                     f"year is provisional; PQA's own totals are as printed.", **_C),
    dict(id="ports.qasim.ships_annual", name="Ships handled at Port Qasim (fiscal year)", unit="count",
         max_value=1e5, dimensions={"level": ["total", "terminal"]},
         description=f"Ships handled at Port Qasim, in total and by terminal, {_FYS}.", **_C),
    dict(id="ports.qasim.teu_annual", name="Port Qasim container traffic (TEUs, fiscal year)", unit="teu",
         max_value=1e8, dimensions={"direction": _DIR},
         description=f"Twenty-foot equivalent units imported and exported through Port Qasim's container "
                     f"terminals, {_FYS} (printed in thousands).", **_C),
]


def fy_end(label: str) -> date | None:
    """'2021-22' -> 2022-06-30. Pure."""
    m = _FY.match(label.strip())
    if not m:
        return None
    y0 = int(m.group(1))
    return date(y0 + 1, 6, 30)


def table_rows(page: str) -> list[list[str]]:
    """The page's biggest table as rows of cell texts. Pure."""
    tables = re.findall(r"(?is)<table.*?</table>", page)
    if not tables:
        raise ValueError("no table on the port performance page")
    out = []
    for r in re.findall(r"(?is)<tr.*?</tr>", max(tables, key=len)):
        cells = [re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", r)]
        if any(cells):
            out.append(cells)
    return out


def _num(t: str) -> float | None:
    t = t.replace(",", "").strip()
    return float(t) if re.fullmatch(r"-?\d+(?:\.\d+)?", t) else None


def _commodity(label: str) -> tuple[str, str | None]:
    """'Crude Oil (Exp.)' -> ('Crude oil', 'export'); 'Edible Oil Imports' -> ('Vegetable oil', 'import'). Pure."""
    direction = None
    if re.search(r"\(\s*\(?\s*exp", label, re.I):
        direction = "export"
    elif re.search(r"\(\s*imp|\bimports?$", label, re.I):
        direction = "import"
    raw = re.sub(r"\(\s*\(?\s*(?:exp|imp)[^)]*\)?|\bimports?$", "", label, flags=re.I).strip(" .-")
    name, _ = ports_pk.commodity(raw, "")
    return name or ports_pk.tidy(raw), direction


def parse(page: str) -> tuple[list[date], list[Record]]:
    """The table -> (fiscal years, records). Pure."""
    rows = table_rows(page)
    years: list[date] = []
    for cells in rows[:4]:
        found = [fy_end(c) for c in cells]
        if sum(1 for f in found if f) >= 3:
            years = [f for f in found if f]
            break
    if not years:
        raise ValueError("no fiscal-year header row")
    n = len(years)
    acc: dict = {}
    items: dict[str, list] = defaultdict(list)
    subtotals: dict[str, list] = {}
    terminal, direction, after_total = None, "import", False

    def add(sid: str, dims: dict, vals: list[float | None], scale: float = 1000.0) -> None:
        for fy, v in zip(years, vals):
            if v is not None:
                acc[(sid, fy, tuple(sorted(dims.items())))] = Record(sid, fy, round(v * scale, 3), dims)

    for cells in rows:
        label = cells[0].strip()
        vals = [_num(c) for c in cells[1:1 + n]] + [None] * max(0, n - len(cells[1:1 + n]))
        has = any(v is not None for v in vals)
        up = re.sub(r"\s+", " ", label.upper())
        if not label:
            continue
        if not has:
            sec = _SECTION.match(up)
            if sec:
                direction = "export" if sec.group(2).startswith("EXP") else "import"
                continue
            term = ports_pk.terminal_in(label)
            if term:
                terminal, after_total = term, False
                direction = "export" if "(EXP" in up else "import"
            continue
        box = bool(terminal and "Container" in terminal)
        cargo = "ports.qasim.cargo_annual"
        if box and "TEU" in up:
            add("ports.qasim.teu_annual", {"direction": "import" if up.startswith("IMP") else "export"}, vals)
        elif box and up.startswith("TOTAL") and "IMP" in up:
            add("ports.qasim.teu_annual", {"direction": "all"}, vals)
        elif up.startswith("WEIGHT OF CONTAINER"):
            add(cargo, {"level": "cargo_type", "direction": "all", "cargo": "containers"}, vals)
            if box:
                add(cargo, {"level": "terminal", "direction": "all", "terminal": terminal}, vals)
        elif re.match(r"NO\.? ?OF SHIPS", up) and terminal:
            add("ports.qasim.ships_annual", {"level": "terminal", "terminal": terminal}, vals, 1.0)
        elif up.startswith("TOTAL NO") and "SHIP" in up:
            add("ports.qasim.ships_annual", {"level": "total"}, vals, 1.0)
        elif up.startswith("GRAND TOTAL"):
            add(cargo, {"level": "total", "direction": "all"}, vals)
        elif up.startswith("TOTAL DRY"):
            add(cargo, {"level": "cargo_type", "direction": "all", "cargo": "dry"}, vals)
            after_total = True
        elif up.startswith("TOTAL LIQUID"):
            add(cargo, {"level": "cargo_type", "direction": "all", "cargo": "liquid"}, vals)
        elif up.startswith("TOTAL") and "IMP" in up and "EXP" in up:
            add(cargo, {"level": "cargo_type", "direction": "all", "cargo": "dry_and_liquid"}, vals)
        elif up.startswith("TOTAL IMP"):
            add(cargo, {"level": "cargo_type", "direction": "import", "cargo": "dry_and_liquid"}, vals)
        elif up.startswith("TOTAL EXP"):
            add(cargo, {"level": "cargo_type", "direction": "export", "cargo": "dry_and_liquid"}, vals)
        elif after_total or not terminal or re.match(r"(DRY|LIQUID) CARGO AT", up):
            continue
        elif up.startswith("SUB") and "Marginal" in terminal:
            continue  # MPT's section sub-totals; its terminal total is 'TOTAL MPT'
        elif up.startswith("SUB") or up.startswith("TOTAL"):
            subtotals[terminal] = vals
            add(cargo, {"level": "terminal", "direction": "all", "terminal": terminal}, vals)
        else:
            name, d = _commodity(label)
            dims = {"level": "commodity", "direction": d or direction, "terminal": terminal, "commodity": name}
            key = ("ports.qasim.cargo_annual", tuple(sorted(dims.items())))
            # one terminal can print a commodity twice ('Furnace Oil' and 'Furance oil'): add them up
            for fy, v in zip(years, vals):
                if v is None:
                    continue
                k = (key[0], fy, key[1])
                prev = acc[k].value if k in acc else 0.0
                acc[k] = Record(key[0], fy, round(prev + v * 1000.0, 3), dims)
            items[terminal].append(vals)
    for term, sub in subtotals.items():
        got = [sum(v[i] or 0 for v in items.get(term, [])) for i in range(n)]
        bad = [years[i].year for i in range(n) if sub[i] is not None and abs(got[i] - sub[i]) > 1.5]
        if bad:
            log.warning("pqa_port_performance: %s items don't add up to its total in FY%s (kept as printed)",
                        term, bad)
    # 'all directions' for each commodity and terminal, where the table splits them
    out = list(acc.values())
    sums: dict = {}
    for r in out:
        if r.dims.get("level") == "commodity":
            dims = {**r.dims, "direction": "all"}
            k = (r.obs_date, tuple(sorted(dims.items())))
            sums[k] = Record(r.series_id, r.obs_date, round((sums[k].value if k in sums else 0.0) + r.value, 3), dims)
    return years, out + list(sums.values())


class PqaPortPerformanceJob(IngestionJob):
    name = "pqa_port_performance"
    source = "PQA"

    def captures(self) -> list[str]:
        import json

        rows = json.loads(self.http_get(CDX_URL).decode("utf-8", "replace") or "[]")[1:]
        return sorted({r[0] for r in rows})

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        errors: list[Exception] = []
        if backfill:
            for ts in self.captures():  # oldest first: a later copy's figure for a year wins
                body = b""
                for attempt in range(4):  # the archive answers a burst with 429s: back off and retry
                    try:
                        body = self.http_get(WAYBACK.format(ts))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
                    if b"<table" in body.lower():
                        out.append(FetchedFile(filename=f"wayback_{ts}.html", content=body, when=date.today()))
                        break
                    time.sleep(15 * (attempt + 1))
                time.sleep(PAUSE_S)
        try:
            out.append(FetchedFile(filename="port-performance.html", content=self.http_get(PAGE_URL),
                                   when=date.today()))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        if errors and not out:
            raise errors[0]
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        return parse(f.content.decode("utf-8", "replace"))[1]
