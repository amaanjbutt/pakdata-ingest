"""kpt_shipping - Karachi Port's daily cargo and container reports: every ship working at a berth, its
cargo discharged and loaded in the 24 hours to 07:30, and every container ship's boxes, each day.

Source: Karachi Port Trust (KPT), Traffic Department — two Oracle Reports PDFs generated on request from
KPT's operations system (linked from https://kpt.gov.pk/pages/78/daily-tonnage and /pages/77/teus-handling):
    http://antares.kpt.gov.pk:90/dev60cgi/rwcgi60.exe?shipcargo   "Statement Showing Daily Tonnage Handling
        During 24 Hrs. ending at 0730 Hrs. on 31-OCT-2026"
    http://antares.kpt.gov.pk:90/dev60cgi/rwcgi60.exe?tues        "Container Handling During 24 Hrs. Ending
        0700 Hrs. on 31-OCT-2026"
Each shows only the latest day — there is no archive (the report keys take no date, and the website's
Vessel History form keeps about three months of arrivals without cargo), so the statements are stored as
they come: history starts the day this job first ran.

Where it runs: antares answers GitHub's runners (6/6) and Cloudflare's network but not the VPS, and
kpt.gov.pk itself answers neither (VPS timeout, runners reset, Cloudflare 522; tested 2026-10-31) — so this
job runs on GitHub Actions (`ports.yml`), and the website's HTML pages are not used.

Daily tonnage (no ruled table; columns by x position): berth, vessel (with the date work commenced and
GRT below), shipping agent, stevedore, the work ("Disc. Crude Oil", "Load Rice", "Dis. / Load
Containers"), then discharging — discharged to date, balance, last 24 hours — and loading — the same
three — gangs (day, night, second day) and last/next ports. Text wraps over several lines, so a ship's row
starts at the line carrying its figures and runs to the next ship's (across page breaks); wharf headings
("East Wharf", "West Wharf" …) group the berths. Container ships add a line of TEUs under the tonnes.
Container handling: one line per container ship — berth, laden and empty 20' and 40' boxes imported and
exported, TEUs and tonnes each way and in total — and a summary of TEUs per terminal (KICT, PICT, SAPT).

Stored:
  * `port_vessel_calls` (port 'KPT'; section berth from the tonnage statement, containers from the
    container report), one row per ship × direction per day — see `ingestion/port_calls.py`.
  * Daily series (module external, dated by the report, 24 hours to 07:30 / 07:00 that day):
        ports.karachi.cargo_handled   t discharged and loaded (level total | commodity | terminal,
                                      direction all | import | export)
        ports.karachi.containers_teu  TEUs handled (level total | terminal, direction, cargo all | laden |
                                      empty)
        ports.karachi.ships_working   ships working at a berth
"""
from __future__ import annotations

import io
import logging
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ingestion import ports_pk
from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.port_calls import PortCallsMixin, VesselRow, number_seqs

log = logging.getLogger("pakdata.ingest")

REPORT_URL = "http://antares.kpt.gov.pk:90/dev60cgi/rwcgi60.exe?{}"
REPORTS = ("shipcargo", "tues", "pplan")
SOURCE_URL = "https://kpt.gov.pk/pages/78/daily-tonnage"
PORT = "KPT"
PKT = timezone(timedelta(hours=5))
LIVE_FROM = date(2026, 11, 1)  # months from here are summed from the daily series; earlier ones: kpt_tonnage_history
MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAY = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_NUM = re.compile(r"^-?[\d,]*\d(?:\.\d+)?$")

# Daily tonnage columns: field -> (left, right) in points, from the report's fixed layout
COLUMNS = {
    "berth": (0, 72), "vessel": (72, 135), "agent": (135, 207), "stevedore": (207, 279), "work": (279, 349),
    "dis": (349, 386), "dis_bal": (386, 418), "dis_24h": (418, 455), "load": (455, 495), "load_bal": (495, 527),
    "load_24h": (527, 563), "gang_day": (563, 595), "gang_night": (595, 622), "gang_2day": (622, 652),
    "last_port": (652, 692), "next_port": (692, 800),
}
FIGS = ("dis", "dis_bal", "dis_24h", "load", "load_bal", "load_24h")
_OP = re.compile(r"^(?:dis(?:c(?:h(?:arge)?)?)?\.?\s*/\s*load|disc?h?\.?|discharg\w*|load(?:ing)?)\b\.?\s*", re.I)
_WHARF_HEAD = re.compile(r"(?:East|West|North|South|New|Old)?\s*(?:Wharf|Oil Piers?|Pier|Jetty|Terminal)", re.I)
_WHARF_TOTAL = re.compile(r"Wharf Wise Total\s*:\s*([\d,]+)\s+([\d,]+)\s+([\d,]+)")
_SUMMARY = re.compile(r"^\s*\d\s+(Containers \(Tons\)|General Cargo|Bulk Cargo|Liquid Cargo)\s+([\d,]+)\s+([\d,]+)\s+"
                      r"([\d,]+)\s*$", re.I | re.M)
_GRAND = re.compile(r"GRAND TOTAL\s*:\s*([\d,]+)\s+([\d,]+)\s+([\d,]+)", re.I)
SUMMARY_CARGO = {"containers (tons)": "containers", "general cargo": "break_bulk", "bulk cargo": "bulk",
                 "liquid cargo": "liquid"}


def report_date(text: str) -> date:
    """'… ending at 0730 Hrs. on 31-OCT-2026' (letters may be spaced out: '3 1 - O C T - 2 0 2 6'). Pure."""
    t = re.sub(r"\s+", "", text)  # undo letter spacing
    m = re.search(r"Hrs\.?on(\d{1,2})-([A-Za-z]{3})-(\d{4})", t, re.I)
    if not m:
        raise ValueError("no report date")
    return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))


def _num(t: str) -> float | None:
    t = t.replace(",", "")
    return float(t) if _NUM.match(t) else None


def _col(x0: float, x1: float) -> str | None:
    cx = (x0 + x1) / 2
    for name, (a, b) in COLUMNS.items():
        if a <= cx < b:
            return name
    return None


def _lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if out and abs(w["top"] - out[-1][0]["top"]) <= tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(ln, key=lambda w: w["x0"]) for ln in out]


def tonnage_rows(pages: list[list[dict]]) -> list[dict]:
    """The daily tonnage statement's ships from each page's words: [{berth, vessel, agent, …, figures,
    teu, wharf}]. A ship starts at a line with figures in both discharge columns. Pure."""
    rows: list[dict] = []
    wharf = None
    for words in pages:
        in_table = False
        for ln in _lines(words):
            text = " ".join(w["text"] for w in ln)
            if text.upper().startswith("SUMMARY"):
                return rows  # the commodity summary that closes the statement
            if not any(_NUM.match(w["text"]) for w in ln) and _WHARF_HEAD.fullmatch(text.strip()):
                wharf = text.strip()  # 'East Wharf', 'West Wharf' — above the column headings
                continue
            if any(w["text"] == "(BULK" for w in ln):
                in_table = True
                continue
            if not in_table or "Wharf Wise Total" in text:
                continue
            cells: dict[str, list[str]] = defaultdict(list)
            for w in ln:
                c = _col(w["x0"], w["x1"])
                if c:
                    cells[c].append(w["text"])
            figs = {c: _num(" ".join(cells[c])) if cells.get(c) else None for c in FIGS}
            if figs["dis"] is not None and figs["dis_bal"] is not None:
                rows.append({"wharf": wharf, "cells": {c: [" ".join(v)] for c, v in cells.items()}, "figs": figs})
                continue
            if text.startswith("Page ") or "Printed on" in text or not rows:
                continue
            r = rows[-1]
            if "TEUs" in text:  # containers: '784 TEUs 47 0 TEUs 0' under the tonnes
                nums = [_num(w["text"]) for w in ln if _num(w["text"]) is not None]
                if len(nums) >= 4:
                    r["teu"] = {"dis": nums[0], "dis_24h": nums[1], "load": nums[2], "load_24h": nums[3]}
                    continue
            for c, v in cells.items():
                if c not in FIGS:
                    r["cells"].setdefault(c, []).append(" ".join(v))
    return rows


def _n(t: str) -> float:
    return float(t.replace(",", ""))


def statement_totals(text: str) -> tuple[list[tuple[float, float]], dict[str, tuple[float, float]]]:
    """The statement's own totals: each wharf's (discharged, loaded) in the 24 hours, in order, and the
    closing summary by cargo type (containers, break_bulk, bulk, liquid, all) as (imports, exports).
    The summary is checked: each line's imports + exports = its total, and the lines add up to the grand
    total; it is dropped ({}) if not. Pure."""
    wharves = [(_n(a), _n(b)) for a, b, c in _WHARF_TOTAL.findall(text) if abs(_n(a) + _n(b) - _n(c)) <= 1]
    summary = {SUMMARY_CARGO[k.lower()]: (_n(i), _n(e)) for k, i, e, t in _SUMMARY.findall(text)
               if abs(_n(i) + _n(e) - _n(t)) <= 1}
    g = _GRAND.search(text)
    if not g or len(summary) != 4:
        return wharves, {}
    gi, ge = _n(g.group(1)), _n(g.group(2))
    if abs(sum(v[0] for v in summary.values()) - gi) > 1 or abs(sum(v[1] for v in summary.values()) - ge) > 1:
        return wharves, {}
    summary["all"] = (gi, ge)
    return wharves, summary


def check_wharves(rows: list[VesselRow], wharves: list[tuple[float, float]]) -> None:
    """Each wharf's ships must add up to its printed 'Wharf Wise Total' (discharged and loaded in the 24
    hours); a wharf that doesn't has its rows marked checks_ok = False (kept, left out of the
    breakdowns). Wharves are matched in the order they are printed."""
    order = list(dict.fromkeys(r.details.get("wharf") for r in rows))
    for wharf, (dis, load) in zip(order, wharves):
        mine = [r for r in rows if r.details.get("wharf") == wharf]
        got_d = sum(r.handled_24h or 0 for r in mine if r.direction == "import")
        got_l = sum(r.handled_24h or 0 for r in mine if r.direction == "export")
        if abs(got_d - dis) > 1 or abs(got_l - load) > 1:
            log.warning("kpt_shipping: %s ships add to %.0f / %.0f, statement says %.0f / %.0f", wharf, got_d,
                        got_l, dis, load)
            for r in mine:
                r.checks_ok = False


def _join(parts: list[str] | None) -> str:
    return re.sub(r"\s+", " ", " ".join(parts or [])).strip()


def tonnage_vessels(day: date, rows: list[dict], source: str) -> list[VesselRow]:
    """Daily tonnage rows -> vessel rows (one per direction worked). Pure."""
    out: list[VesselRow] = []
    for r in rows:
        c = r["cells"]
        berth = re.sub(r"-\s+(?=\d)", "-", _join(c.get("berth")))
        vparts = c.get("vessel", [])
        name_parts, commenced, grt = [], None, None
        for p in vparts:
            m = _DAY.search(p)
            if m and commenced is None:
                commenced = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
                continue
            g = re.search(r"GRT\s*=\s*([\d,]+)", p)
            if g:
                grt = float(g.group(1).replace(",", ""))
                continue
            if not _DAY.search(p) and "@" not in p:
                name_parts.append(p)
        vessel = re.sub(r"^(?:M\.?\s*[TV]\.?\s*)", "", _join(name_parts)).strip(" .")
        work = _join([p for p in c.get("work", []) if not _DAY.search(p) and "@" not in p])
        g2 = re.search(r"GRT\s*=\s*([\d,]+)", " ".join(vparts + c.get("berth", [])))
        if grt is None and g2:
            grt = float(g2.group(1).replace(",", ""))
        op = _OP.match(work)
        what = work[op.end():].strip() if op else work
        opword = (op.group(0).lower() if op else "")
        f = r["figs"]
        details = {k: v for k, v in (
            ("stevedore", _join(c.get("stevedore"))), ("work", work), ("grt", grt),
            ("gangs", {k: _num(_join(c.get(k))) for k in ("gang_day", "gang_night", "gang_2day")}),
            ("last_port", _join(c.get("last_port"))), ("next_port", _join(c.get("next_port"))),
            ("commenced", commenced.isoformat() if commenced else None)) if v}
        agent = _join(c.get("agent"))
        want = []
        if "dis" in opword or (f["dis"] or f["dis_bal"] or f["dis_24h"]):
            want.append(("import", "dis"))
        if "load" in opword or (f["load"] or f["load_bal"] or f["load_24h"]):
            want.append(("export", "load"))
        for direction, k in want or [("", "dis")]:
            done, bal, h24 = f[k] or 0.0, f[k + "_bal"] or 0.0, f[k + "_24h"] or 0.0
            name, unit = ports_pk.commodity(what, "M.Ton")
            row = VesselRow(port=PORT, statement_date=day, section="berth", vessel=vessel, direction=direction,
                            commodity_raw=what, commodity=name, unit="metric ton" if unit != "cubic metre" else unit,
                            berth_raw=berth, terminal=ports_pk.kpt_terminal(berth, r["wharf"]), agent_raw=agent or None,
                            agent=ports_pk.agent(agent), vessel_type=ports_pk.kpt_vessel_type(what),
                            manifest=done + bal, handled_24h=h24, handled_before=done - h24, balance=bal,
                            checks_ok=done >= h24, details=dict(details), source_file=source)
            if r.get("teu"):
                t = r["teu"]
                row.details["teu"] = t[k]
                row.details["teu_24h"] = t[k + "_24h"]
            if r["wharf"]:
                row.details["wharf"] = r["wharf"]
            out.append(row)
    return out


_TEU_ROW = re.compile(r"^\s*\d{1,3}\s+(.+?)\s+((?:[A-Za-z]+-?\d+[A-Za-z]?(?:/[A-Za-z]+-?\d+)?)|[A-Z]{2,}\S*)\s+"
                      r"((?:-?[\d,]+\s+){13}-?[\d,]+)\s*$")
_TEU_SUM = re.compile(r"^\s*(KICT|PICT|SAPTL?|QICT|[A-Z]{3,6})\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s*$")


def container_lines(text: str) -> tuple[list[dict], dict[str, dict]]:
    """The container report's text -> (ships, terminal summary). Each ship: vessel, berth and 14 counts
    (import laden 20'/40', empty 20'/40', export the same, import TEUs/tonnes, export TEUs/tonnes,
    total TEUs/tonnes); a vessel name that wraps continues on the next line. Pure."""
    ships: list[dict] = []
    summary: dict[str, dict] = {}
    in_summary = False
    for line in text.splitlines():
        if line.strip().upper().startswith("SUMMARY"):
            in_summary = True
            continue
        if in_summary:
            m = _TEU_SUM.match(line.replace(",", ""))
            if m and m.group(1) not in ("TOTAL", "GRAND"):
                v = [float(x) for x in m.groups()[1:]]
                summary[m.group(1)] = {"import_laden": v[0], "import_empty": v[1], "export_laden": v[2],
                                       "export_empty": v[3], "total": v[4]}
            continue
        m = _TEU_ROW.match(line)
        if m:
            n = [float(x.replace(",", "")) for x in m.group(3).split()]
            ships.append({"vessel": m.group(1), "berth": m.group(2), "n": n})
        elif ships and re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,30}", line.strip()) and not line.strip().startswith(
                ("Total", "SUMMARY", "IMPORTS", "Terminal", "Print", "Page", "Laden", "Serial", "No.")):
            ships[-1]["vessel"] += " " + line.strip()
    return ships, summary


def container_vessels(day: date, ships: list[dict], source: str) -> list[VesselRow]:
    out: list[VesselRow] = []
    for s in ships:
        n = s["n"]
        for direction, off, tt in (("import", 0, 8), ("export", 4, 10)):
            l20, l40, e20, e40 = n[off:off + 4]
            teu, tonnes = n[tt], n[tt + 1]
            if not teu and not tonnes and not any((l20, l40, e20, e40)):
                continue
            ok = abs(l20 + 2 * l40 + e20 + 2 * e40 - teu) <= 1
            out.append(VesselRow(port=PORT, statement_date=day, section="containers", vessel=s["vessel"],
                                 direction=direction, commodity_raw="CONTAINERS", commodity="Containers", unit="teu",
                                 berth_raw=s["berth"], terminal=ports_pk.kpt_terminal(s["berth"], None),
                                 handled_24h=teu, checks_ok=ok, source_file=source,
                                 details={"laden_20": l20, "laden_40": l40, "empty_20": e20, "empty_40": e40,
                                          "tonnes": tonnes}))
    return out


_PPLAN_TYPE = re.compile(r"^Vessel Type:\s*(.+)$")
_PPLAN_ROW = re.compile(r"^(\d{10}/\d{2})\s+(\d{2,3}(?:\.\d+)?)\s*(.+?)\s+(\d{1,2}(?:\.\d+)?)\s+(\d{4})/(\d{2})\b(.*)$")
_QTY = re.compile(r"\b([DL])/(\d[\d,]*)\s+([A-Z][A-Z0-9 ()&.,/-]*?)(?=\s+(?:[A-Z]{1,3}[-/]|B N R|Op-|$)|$)")
_PROG_DATE = re.compile(r"SHIPPING PROGRAMME,\s*([A-Z]+)\s+(\d{1,2}),\s*(\d{4})", re.I)


def _reg_day(reg: str) -> date | None:
    try:
        return date(2000 + int(reg[4:6]), int(reg[2:4]), int(reg[:2]))
    except ValueError:
        return None


def arrival(reg: str, hhmm: str, dd: str, programme: date) -> datetime | None:
    """'1042' + '06' (day of month) -> the first such day on or after the call's registration date
    (its number starts DDMMYY), else the nearest one on or before the programme date. Pure."""
    day, start = int(dd), _reg_day(reg) or programme - timedelta(days=40)
    d = start
    for _ in range(400):
        if d.day == day:
            break
        d += timedelta(days=1)
    else:
        return None
    h, m = int(hhmm[:2]), int(hhmm[2:])
    return datetime(d.year, d.month, d.day, min(h, 23), min(m, 59))


def programme_rows(text: str, source: str) -> tuple[date, list[VesselRow]]:
    """KPT's shipping programme (ships expected and waiting, by vessel type) -> one row per ship × cargo
    line ('D/58488 WHEAT' = discharge 58,488 t; 'L/25000 HSFO' = load); container ships carry no quantity.
    Pure."""
    m = _PROG_DATE.search(text)
    if not m:
        raise ValueError("no programme date")
    day = date(int(m.group(3)), MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    out: list[VesselRow] = []
    vtype, last = None, None
    for line in text.splitlines():
        line = line.strip()
        t = _PPLAN_TYPE.match(line)
        if t:
            vtype = t.group(1).strip().title()
            continue
        r = _PPLAN_ROW.match(line)
        if r:
            reg, loa, name, draft, hhmm, dd, rest = r.groups()
            name = re.sub(r"^M\.?\s*[TV]\.?\s*", "", name).strip(" .")
            base = dict(port=PORT, statement_date=day, section="programme", vessel=name, call_ref=reg,
                        vessel_type=vtype, loa_m=float(loa), draft_m=float(draft),
                        expected_at=arrival(reg, hhmm, dd, day), source_file=source)
            berth = re.search(r"\b([EW]/(?:SAPT|KICT|PICT|KGTL)|Op-\d|B-\d+(?:\+B-\d+)?)\b", rest)
            base["berth_raw"] = berth.group(1) if berth else None
            base["terminal"] = ports_pk.kpt_terminal(base["berth_raw"].split("/")[-1], None) if berth else None
            last = base
            qty = _QTY.findall(rest)
            if not qty:
                out.append(VesselRow(**base, details={"request": rest.strip()}))
            for op, q, what in qty:
                out.append(_programme_cargo(base, op, q, what, rest))
            continue
        if last:
            for op, q, what in _QTY.findall(line):  # a second cargo on its own line ('D/5500 LENTILS')
                out.append(_programme_cargo(last, op, q, what, line))
    return day, out


def _programme_cargo(base: dict, op: str, q: str, what: str, rest: str) -> VesselRow:
    name, unit = ports_pk.commodity(what.strip(), "")
    if re.fullmatch(r"CARS?|VEHICLES?|UNITS?", what.strip().upper()):
        name, unit = "Vehicles (ro-ro)", "units"
    return VesselRow(**base, direction="import" if op == "D" else "export", commodity_raw=what.strip(),
                     commodity=name, unit=unit, manifest=_n(q), details={"request": rest.strip()})


_C = dict(module="external", frequency="daily", source="KPT", source_url=SOURCE_URL, min_value=0)
CARGO_TYPES = ["all", "general", "bulk", "dry", "liquid", "containers", "break_bulk"]
_DIR = ["all", "import", "export"]
_AT = ("in the 24 hours to 07:30 each day (Karachi Port Trust's daily tonnage statement; history from "
       "November 2026, when PakDataHub began storing it)")
CATALOG = [
    dict(id="ports.karachi.cargo_handled", name="Karachi Port cargo handled (24 hours)", unit="metric ton",
         max_value=2e6, dimensions={"level": ["total", "cargo_type", "commodity", "terminal"], "direction": _DIR,
                                    "cargo": CARGO_TYPES},
         description="Tonnes of cargo discharged (import) and loaded (export) at Karachi Port in the 24 hours to "
                     "07:30, by direction (dimension direction) and by cargo type (level cargo_type, dimension "
                     "cargo: general — general cargo including containers —, bulk, dry — the two together —, "
                     "liquid; from November 2026 also containers and break_bulk), commodity or wharf (level "
                     "commodity, terminal; from November 2026). Daily since July 2020: Karachi Port Trust's "
                     "tonnage report, rounded to the nearest 1,000 t, until November 2026; its daily tonnage "
                     "statement, to the tonne, since. Container ships count the weight of their boxes.", **_C),
    dict(id="ports.karachi.cargo_handled_monthly", name="Karachi Port cargo handled (monthly)", unit="metric ton",
         frequency_override="monthly", max_value=3e7,
         dimensions={"level": ["total", "cargo_type"], "direction": _DIR, "cargo": CARGO_TYPES[:5]},
         description="Tonnes of cargo discharged and loaded at Karachi Port each month, by direction and cargo "
                     "type, from Karachi Port Trust's tonnage report (July 2020 → October 2026) and the sum of "
                     "its daily statements since. Dated the last day of the month.", **_C),
    dict(id="ports.karachi.containers_teu_monthly", name="Karachi Port container traffic (monthly TEUs)",
         unit="teu", frequency_override="monthly", max_value=2e6, dimensions={"direction": _DIR},
         description="Twenty-foot equivalent units imported and exported at Karachi Port each month, from Karachi "
                     "Port Trust's tonnage report (July 2020 → October 2026, to the nearest 1,000 TEU) and the "
                     "sum of its daily container reports since. Dated the last day of the month.", **_C),
    dict(id="ports.karachi.ships_scheduled", name="Ships in Karachi Port's shipping programme", unit="count",
         max_value=500, dimensions={"level": ["total", "vessel_type"], "direction": _DIR},
         description="Ships listed in Karachi Port Trust's daily shipping programme — expected or waiting to "
                     "berth — by the direction of their cargo and by vessel type (wheat, tanker, container ship, "
                     "clinkers …), each day from November 2026.", **_C),
    dict(id="ports.karachi.cargo_scheduled", name="Cargo in Karachi Port's shipping programme", unit="metric ton",
         max_value=5e7, dimensions={"level": ["total", "commodity"], "direction": _DIR},
         description="Tonnes to be discharged or loaded by the ships in Karachi Port Trust's daily shipping "
                     "programme, by direction and commodity, each day from November 2026. Container ships, "
                     "which list no quantity, and vehicles (counted in units) are left out.", **_C),
    dict(id="ports.karachi.containers_teu", name="Karachi Port containers handled (TEUs, 24 hours)", unit="teu",
         max_value=1e5, dimensions={"level": ["total", "terminal"], "direction": _DIR,
                                    "cargo": ["all", "laden", "empty"]},
         description="Twenty-foot equivalent units (TEUs) imported and exported at Karachi Port's container "
                     "terminals (KICT, PICT, SAPT) in the 24 hours to 07:00, laden and empty (dimension cargo), "
                     "from Karachi Port Trust's daily container-handling report since November 2026; the total "
                     "(level total, cargo all) from its tonnage report, to the nearest 1,000 TEU, back to July "
                     "2020.",
         **_C),
    dict(id="ports.karachi.ships_working", name="Ships working at Karachi Port", unit="count", max_value=300,
         dimensions={"level": ["total", "terminal"]},
         description=f"Ships working cargo at Karachi Port's berths, by wharf, {_AT}.", **_C),
]
for _c in CATALOG:
    _c["frequency"] = _c.pop("frequency_override", _c["frequency"])


def _add(acc: dict, sid: str, day: date, dims: dict, v: float) -> None:
    k = (sid, tuple(sorted(dims.items())))
    acc[k] = Record(sid, day, (acc[k].value if k in acc else 0.0) + v, dims)


def cargo_type_records(day: date, summary: dict[str, tuple[float, float]], sid: str) -> list[Record]:
    """The statement's (or the tonnage report's) totals by cargo type -> records. `summary` maps cargo type
    (all, containers, break_bulk, bulk, liquid — or general, bulk, dry, liquid, all) to (imports, exports).
    Pure."""
    v = dict(summary)
    if "containers" in v and "break_bulk" in v:
        v["general"] = (v["containers"][0] + v["break_bulk"][0], v["containers"][1] + v["break_bulk"][1])
    if "general" in v and "bulk" in v:
        v.setdefault("dry", (v["general"][0] + v["bulk"][0], v["general"][1] + v["bulk"][1]))
    acc: dict = {}
    for cargo, (imp, exp) in v.items():
        for d, x in (("import", imp), ("export", exp), ("all", imp + exp)):
            dims = {"level": "total", "direction": d} if cargo == "all" else \
                {"level": "cargo_type", "direction": d, "cargo": cargo}
            _add(acc, sid, day, dims, x)
    return list(acc.values())


def tonnage_records(day: date, rows: list[VesselRow], summary: dict) -> list[Record]:
    acc: dict = {}
    sid = "ports.karachi.cargo_handled"
    for rec in cargo_type_records(day, summary, sid):
        acc[(rec.series_id, tuple(sorted(rec.dims.items())))] = rec
    for r in rows:
        if r.handled_24h is None or r.checks_ok is False or r.direction not in ("import", "export"):
            continue
        for d in ("all", r.direction):
            if r.commodity:
                _add(acc, sid, day, {"level": "commodity", "direction": d, "commodity": r.commodity}, r.handled_24h)
            if r.terminal:
                _add(acc, sid, day, {"level": "terminal", "direction": d, "terminal": r.terminal}, r.handled_24h)
    ships = {(r.vessel, r.berth_raw): r.terminal for r in rows}
    _add(acc, "ports.karachi.ships_working", day, {"level": "total"}, float(len(ships)))
    for t in ships.values():
        if t:
            _add(acc, "ports.karachi.ships_working", day, {"level": "terminal", "terminal": t}, 1.0)
    return list(acc.values())


def programme_records(day: date, rows: list[VesselRow]) -> list[Record]:
    acc: dict = {}
    ships: dict[tuple, set] = defaultdict(set)
    for r in rows:
        dirs = ["all"] + ([r.direction] if r.direction else [])
        for d in dirs:
            ships[(("level", "total"), ("direction", d))].add(r.vessel)
            if r.vessel_type:
                ships[(("level", "vessel_type"), ("direction", d), ("vessel_type", r.vessel_type))].add(r.vessel)
            if r.unit == "metric ton" and r.manifest:
                _add(acc, "ports.karachi.cargo_scheduled", day, {"level": "total", "direction": d}, r.manifest)
                if r.commodity:
                    _add(acc, "ports.karachi.cargo_scheduled", day,
                         {"level": "commodity", "direction": d, "commodity": r.commodity}, r.manifest)
    for k, v in ships.items():
        _add(acc, "ports.karachi.ships_scheduled", day, dict(k), float(len(v)))
    return list(acc.values())


def container_records(day: date, summary: dict[str, dict]) -> list[Record]:
    acc: dict = {}
    sid = "ports.karachi.containers_teu"
    for term, v in summary.items():
        name = ports_pk.kpt_terminal(term, None) or term
        for d in ("import", "export"):
            for cargo in ("laden", "empty"):
                x = v[f"{d}_{cargo}"]
                for dims in ({"level": "total"}, {"level": "terminal", "terminal": name}):
                    for dd, cc in ((d, cargo), (d, "all"), ("all", cargo), ("all", "all")):
                        _add(acc, sid, day, {**dims, "direction": dd, "cargo": cc}, x)
    return list(acc.values())


def month_totals(today: date) -> list[Record]:
    """Monthly totals for each complete month since LIVE_FROM, summed from the daily series: a dims
    combination is summed only if it has a value on every day of the month."""
    from app import db

    out: list[Record] = []
    m = LIVE_FROM
    while True:
        end = date(m.year, m.month, monthrange(m.year, m.month)[1])
        if end >= today:
            return out
        rows = db.query(
            "SELECT series_id, dims, count(*) AS n, sum(value) AS v FROM observations "
            "WHERE series_id IN ('ports.karachi.cargo_handled', 'ports.karachi.containers_teu') "
            "AND obs_date BETWEEN %(a)s AND %(b)s AND value IS NOT NULL GROUP BY series_id, dims",
            {"a": m, "b": end})
        for r in rows:
            dims = r["dims"]
            if r["n"] != end.day or dims.get("level") not in ("total", "cargo_type"):
                continue
            if r["series_id"] == "ports.karachi.cargo_handled":
                out.append(Record("ports.karachi.cargo_handled_monthly", end, float(r["v"]), dims))
            elif dims.get("level") == "total" and dims.get("cargo") == "all":
                out.append(Record("ports.karachi.containers_teu_monthly", end, float(r["v"]),
                                  {"direction": dims["direction"]}))
        m = end + timedelta(days=1)


class KptShippingJob(PortCallsMixin, IngestionJob):
    name = "kpt_shipping"
    source = "KPT"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        errors: list[Exception] = []
        for key in REPORTS:
            try:
                content = self.http_get(REPORT_URL.format(key))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                continue
            if content[:4] != b"%PDF":
                errors.append(ValueError(f"{key}: not a PDF"))
                continue
            out.append(FetchedFile(filename=f"kpt_{key}.pdf", content=content, when=date.today()))
        if errors and not out:
            raise errors[0]
        return out

    def parse(self, f: FetchedFile) -> list:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            if "pplan" in f.filename:
                day, rows = programme_rows(text, f.filename)
                rows = number_seqs(rows)
                if not rows:
                    raise ValueError("no ships in the shipping programme")
                # KPT posts the next day's programme the evening before: its ships are kept, its series
                # wait until the day arrives (the morning run writes them)
                return rows + (programme_records(day, rows) if day <= date.today() else [])
            day = report_date(text[:600])
            if datetime.now(PKT) < datetime(day.year, day.month, day.day, 8, tzinfo=PKT):
                # from midnight KPT's reports carry the next 24 hours (to 07:30), still adding up: not yet
                log.info("kpt_shipping: %s report for %s still in progress — skipped", f.filename, day)
                return []
            if "shipcargo" in f.filename:
                rows = number_seqs(tonnage_vessels(day, tonnage_rows([p.extract_words() for p in pdf.pages]),
                                                   f.filename))
                if not rows:
                    raise ValueError("no ships in the daily tonnage statement")
                wharves, summary = statement_totals(text)
                if not summary:
                    raise ValueError("the statement's commodity summary is missing or doesn't add up")
                check_wharves(rows, wharves)
                return rows + tonnage_records(day, rows, summary)
        ships, summary = container_lines(text)
        rows = number_seqs(container_vessels(day, ships, f.filename))
        if not summary:
            raise ValueError("no terminal summary in the container report")
        return rows + container_records(day, summary)

    def upsert(self, validated) -> int:
        n = super().upsert(validated)
        monthly = month_totals(date.today())  # complete months, from the days now stored
        return n + (IngestionJob.upsert(self, IngestionJob.validate(self, monthly)) if monthly else 0)
