"""ffd_river_flows - river flows at Pakistan's dams, barrages and gauges, every day of the flood season.

Source: Pakistan Meteorological Department, Flood Forecasting Division (FFD, Lahore), daily flood
bulletin — https://ffd.pmd.gov.pk/bulletin. One bulletin a day from 15 June to 15 October (into
late October in 2024), issued around 11:00 PST, with the "actual observations at 0600 PST" for 22
gauging stations on the Indus, Kabul, Jhelum, Chenab, Ravi and Sutlej (inflow and outflow, in
thousands of cusecs) and the Tarbela and Mangla reservoir levels (feet) and live storage (%).

Where the bulletins are:
- this season: https://ffd.pmd.gov.pk/bulletin/bulletin?page=N lists them, newest first; each one's
  text is at /bulletin/<id>/body (an HTML fragment, ~110 KB — the PDF is ~4 MB).
- past seasons (2024 and 2025 so far): https://ffd.pmd.gov.pk/bulletins/archive?type=bulletin&page=N,
  PDFs at /bulletins/archive/<id>/download (text layer, two or three pages).
The live map (/river-state) draws on a token-gated endpoint the page marks as anti-scraping; it is
not used. The bulletins are FFD's published record of the 06:00 readings.

Layouts: the 2024 and 2025 PDFs and the June–July 2026 bulletins print a "Design Capacity" column
between the station and its inflow ("Tarbela 1500 285.0 255.3 300-330 Low …"; "-"/"--" where
there is none); from August 2026 the column is gone ("INDUS Tarbela 44.0 55.2 40 - 45 …"). A
document is read with the capacity column when most of its station rows start with a whole number
or a dash. Station rows are the first line naming a known station (`ingestion/water_pk.py`)
followed by numbers; the later "how close is each river to its limit" chart repeats the stations
with other figures and is ignored. Archive PDFs bind each day's covering letter (part A) to a
table (part B) that is sometimes another day's — 13 Oct 2024's file holds B-111/24 of 3 Oct, whose
own date and serial agree — so a bulletin is dated by the "Dated:" line heading its table. A few
2025 tables overlap their words when read by position ("CHENAMarala"); those are re-read in
writing order. Cells can wrap onto the next line; blank or "NR" cells leave that station out.
Reservoir levels are told apart by range (Tarbela 1,300–1,560 ft, Mangla 1,000–1,250 ft); "Live
Storage 98.0%" / "Storage 69.44% live" are read in print order (Tarbela, then Mangla).

Series (dims `site`, `river`, `measure`), dated as on the bulletin's day (0600 PST readings):
    water.ffd.river_flow        cusecs — measure inflow | outflow, every station
    water.ffd.reservoir_level   feet above sea level — site Tarbela | Mangla
    water.ffd.reservoir_storage % of live storage capacity filled — site Tarbela | Mangla
ffd.pmd.gov.pk answers the VPS: a daily run re-reads the newest three bulletins (off-season too,
so the run stays healthy — from the archive if this season's list is empty); `--backfill` reads
the archive and this season (376 documents → 362 days, 2024-06-15 → 2026-10-15, checked
2026-11-01). Bulletins never restate earlier days, so there are no revisions to track.
"""
from __future__ import annotations

import html as htmllib
import io
import logging
import re
import time
from datetime import date

from ingestion import water_pk
from ingestion.framework import FetchedFile, IngestionJob, Record

log = logging.getLogger("pakdata.ingest")

BASE = "https://ffd.pmd.gov.pk"
CURRENT_LIST = BASE + "/bulletin/bulletin?page={page}"
CURRENT_BODY = BASE + "/bulletin/{id}/body"
ARCHIVE_LIST = BASE + "/bulletins/archive?type=bulletin&page={page}"
ARCHIVE_PDF = BASE + "/bulletins/archive/{id}/download"
SOURCE_URL = BASE + "/bulletin"
LATEST_N = 3
MAX_PAGES = 40
PAUSE_S = 1.0
MIN_STATIONS = 12  # a bulletin prints 22-23; fewer means the table didn't parse

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DATED = re.compile(r"Dated\s*:?\s*-?\s*(\d{1,2})\s*(?:st|nd|rd|th)?[\s.,\-]*([A-Za-z]{3,9})[\s.,\-]*(\d{4})", re.I)
_NUM = re.compile(r"^-?\d[\d,]*(?:\.\d+)?$")
_FLOW = re.compile(r"^\d[\d,]*\.\d{1,2}$")  # flows are printed "285.0"; forecasts "300-330", "15"
_DASH = re.compile(r"^-{1,2}$|^—$")
_RIVER_WORDS = {r.upper() for r in water_pk.RIVERS}
_STOP = re.compile(r"HOW CLOSE IS EACH RIVER", re.I)
_TABLE = re.compile(r"QUANTITATIVE|Actual\s+Observations", re.I)
_LEVEL = re.compile(r"(?:Current\s*:|Level)\s*\|?\s*(1[0-5]\d\d(?:\.\d+)?)", re.I)
_STORAGE = re.compile(r"(?:Live\s+Storage\s*\|?\s*([\d.]+)\s*%|Storage\s*\|?\s*([\d.]+)\s*%\s*live)", re.I)
_RESERVOIR_FT = {"Tarbela": (1300.0, 1560.0), "Mangla": (1000.0, 1250.0)}

_C = dict(module="real", frequency="daily", source="PMD Flood Forecasting Division", source_url=SOURCE_URL)
_SITES = [s.name for s in water_pk.SITES]
_G = ("at 06:00 PST on each day of the flood season (15 June to mid-October), from the daily flood "
      "bulletin of the Pakistan Meteorological Department's Flood Forecasting Division, 2024 →")
CATALOG = [
    dict(id="water.ffd.river_flow", name="River flow at dams, barrages and gauges (FFD, 06:00)",
         unit="cusecs", min_value=0, max_value=2e6,
         dimensions={"site": _SITES, "measure": ["inflow", "outflow"]},
         description=f"Water flowing into (inflow) and out of (outflow) each of the {len(_SITES)} gauging "
                     f"stations on the Indus, Kabul, Jhelum, Chenab, Ravi and Sutlej — Tarbela, Kalabagh, "
                     f"Chashma, Taunsa, Guddu, Sukkur, Kotri, Nowshera, Mangla, Rasul, Marala, Khanki, "
                     f"Qadirabad, Chiniot Bridge, Trimmu, Panjnad, Jassar, Shahdara, Balloki, Sidhnai, Ganda "
                     f"Singh Wala, Sulemanki and Islam — in cusecs (cubic feet per second), {_G}. Dimensions "
                     f"site, river, measure (inflow | outflow). For a barrage, inflow is the river upstream "
                     f"and outflow what passes downstream after the canals draw off.", **_C),
    dict(id="water.ffd.reservoir_level", name="Tarbela and Mangla reservoir level (FFD, 06:00)",
         unit="ft", min_value=1000, max_value=1600,
         dimensions={"site": ["Tarbela", "Mangla"]},
         description=f"Water level of the Tarbela (Indus) and Mangla (Jhelum) reservoirs in feet above sea "
                     f"level, {_G}. Tarbela's dead level is 1,380 ft and its maximum conservation level "
                     f"1,550 ft; Mangla's are 1,040 ft and 1,242 ft. Dimensions site, river.", **_C),
    dict(id="water.ffd.reservoir_storage", name="Tarbela and Mangla live storage filled (FFD, 06:00)",
         unit="percent", min_value=0, max_value=100,
         dimensions={"site": ["Tarbela", "Mangla"]},
         description=f"Share of the Tarbela and Mangla reservoirs' live storage capacity that is filled, in "
                     f"per cent, {_G}. Dimensions site, river.", **_C),
]


def bulletin_date(text: str) -> date:
    m = _DATED.search(text)
    if not m or m.group(2)[:3].lower() not in MONTHS:
        raise ValueError("no 'Dated:' line in the bulletin")
    return date(int(m.group(3)), MONTHS[m.group(2)[:3].lower()], int(m.group(1)))


def html_lines(page: str) -> list[str]:
    """An HTML bulletin -> text lines, one per table row (cells joined by spaces). Pure."""
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<svg.*?</svg>", " ", page, flags=re.S | re.I)

    def row(m: re.Match) -> str:
        cells = [re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(0), re.S | re.I)]
        return "\n" + " ".join(c for c in cells if c) + "\n"

    t = re.sub(r"<tr[^>]*>.*?</tr>", row, t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</(?:p|div|h\d|li|table)>", "\n", t, flags=re.I)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", t))
    return [ln for ln in (re.sub(r"\s+", " ", x).strip() for x in t.splitlines()) if ln]


def pdf_lines(content: bytes, text_flow: bool = False) -> list[str]:
    """A PDF's text lines. `text_flow` reads characters in the order they were written instead of
    by position — it untangles the few tables whose words overlap ("CHENAMarala", "Is i lam")."""
    import pdfplumber

    kw = {"use_text_flow": True, "x_tolerance": 1} if text_flow else {}
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return [ln.strip() for p in pdf.pages for ln in (p.extract_text(**kw) or "").splitlines() if ln.strip()]


def parse_pdf(content: bytes) -> dict | None:
    """`parse_lines` over the PDF's text by position, or in writing order when that reads more
    stations (a table that fails or reads short by position is tried both ways)."""
    try:
        b = parse_lines(pdf_lines(content))
        if b is None or len(b["flows"]) >= 20:
            return b
    except ValueError as exc:
        b, err = None, exc
    else:
        err = None
    try:
        alt = parse_lines(pdf_lines(content, text_flow=True))
    except ValueError:
        alt = None
    best = max((x for x in (b, alt) if x), key=lambda x: len(x["flows"]), default=None)
    if best is None and err is not None:
        raise err
    return best


def _num(tok: str) -> float | None:
    return float(tok.replace(",", "")) if _NUM.match(tok) else None


def station_row(line: str) -> tuple[water_pk.Site, list[str]] | None:
    """'INDUS Kalabagh 950 50.9 44.7 No sig. change …' -> (Kalabagh, ['950', '50.9', '44.7', …]):
    a known station's name (after an optional river name) followed by a number or a dash. Pure."""
    toks = line.replace("|", " ").split()
    if toks and toks[0].upper() in _RIVER_WORDS:
        toks = toks[1:]
    for n in (3, 2, 1):  # longest name first: "G. S. Wala", "Chiniot Bridge"
        if len(toks) <= n or any(re.search(r"[\d\-]", t) for t in toks[:n]):
            continue
        site = water_pk.canon_site(" ".join(toks[:n]))
        rest = toks[n:]
        if site and (_num(rest[0]) is not None or _DASH.match(rest[0])):
            return site, rest
    return None


def parse_lines(lines: list[str]) -> dict | None:
    """Bulletin text lines -> {"date", "flows": {site: (inflow, outflow)} in thousand cusecs,
    "levels": {site: ft}, "storage": {site: %}, "dropped": [site, ...]}; None for a bulletin
    without the gauge table (the season's first one, 15 June 2024, is a letter and a weekly
    outlook). A station whose two cells aren't printed flows (blank cells leave the forecast,
    "Mangla 1060 15 -30" on 7 Oct 2025; "NR NR") is dropped from that day, not the whole bulletin;
    cells wrapped onto the next line ("Tarbela 1500" / "151.0 155.3") are picked up from there.

    The date is the "Dated:" line that heads the table's own page: the archive's PDFs bind the
    covering letter of one day to the table of another (13 Oct 2024's letter + 3 Oct's table). Pure."""
    rows: dict[str, tuple[int, list[str]]] = {}
    sites: dict[str, water_pk.Site] = {}
    stop = len(lines)
    for i, ln in enumerate(lines):
        if _STOP.search(ln):
            stop = i
            break
        hit = station_row(ln)
        if hit and hit[0].name not in rows:
            rows[hit[0].name] = (i, hit[1])
            sites[hit[0].name] = hit[0]
    first = min((i for i, _ in rows.values()), default=None)
    head = next((i for i in range(first, -1, -1) if _DATED.search(lines[i])), None) if first is not None else None
    when = bulletin_date(lines[head] if head is not None else "\n".join(lines[:80]))
    lead = [toks[0] for _, toks in rows.values()]
    capacity = sum(bool(_DASH.match(t)) or (_num(t) is not None and "." not in t) for t in lead) > len(lead) / 2
    flows: dict[str, tuple[float, float]] = {}
    dropped: list[str] = []
    for name, (i, toks) in rows.items():
        if capacity:
            toks = toks[1:]
        pair = toks[:2] if len(toks) >= 2 and all(_FLOW.match(t) for t in toks[:2]) else None
        for nxt in lines[i + 1:i + 3] if pair is None else ():
            more = nxt.replace("|", " ").split()
            if station_row(nxt) is None and len(more) >= 2 and all(_FLOW.match(t) for t in more[:2]):
                pair = more[:2]
                break
        vals = [_num(t) for t in pair] if pair else None
        if vals is None or max(vals) > 1500:
            dropped.append(name)
            continue
        flows[name] = (vals[0], vals[1])
    if not rows and not any(_TABLE.search(ln) for ln in lines):
        return None
    if len(flows) < MIN_STATIONS:
        raise ValueError(f"only {len(flows)} station rows found")
    text = "\n".join(lines[head or 0:stop])
    levels: dict[str, float] = {}
    for m in _LEVEL.finditer(text):
        v = float(m.group(1))
        for site, (lo, hi) in _RESERVOIR_FT.items():
            if lo <= v <= hi and site not in levels:
                levels[site] = v
    pcts = [float(a or b) for a, b in _STORAGE.findall(text)]
    storage = dict(zip(("Tarbela", "Mangla"), pcts)) if len(pcts) == 2 and all(0 <= p <= 100 for p in pcts) else {}
    return {"date": when, "flows": flows, "levels": levels, "storage": storage, "sites": sites,
            "dropped": dropped}


def to_records(b: dict) -> list[Record]:
    """Parsed bulletin -> records (flows in cusecs). Pure."""
    out: list[Record] = []
    when = b["date"]
    for name, (inflow, outflow) in b["flows"].items():
        river = b["sites"][name].river
        for measure, v in (("inflow", inflow), ("outflow", outflow)):
            out.append(Record("water.ffd.river_flow", when, round(v * 1000.0, 3),
                              {"site": name, "river": river, "measure": measure}))
    for name, v in b["levels"].items():
        out.append(Record("water.ffd.reservoir_level", when, v,
                          {"site": name, "river": water_pk.canon_site(name).river}))
    for name, v in b["storage"].items():
        out.append(Record("water.ffd.reservoir_storage", when, v,
                          {"site": name, "river": water_pk.canon_site(name).river}))
    return out


def current_ids(page: str) -> list[int]:
    """Bulletin ids on a /bulletin/bulletin listing page. Pure."""
    return sorted({int(i) for i in re.findall(r"/bulletin/(\d+)/download", page)})


def archive_ids(page: str) -> list[int]:
    """Bulletin ids on a /bulletins/archive listing page. Pure."""
    return sorted({int(i) for i in re.findall(r"/bulletins/archive/(\d+)/download", page)})


class FfdRiverFlowsJob(IngestionJob):
    name = "ffd_river_flows"
    source = "PMD-FFD"

    def _listed(self, url: str, ids_of) -> list[int]:
        seen: list[int] = []
        for page in range(1, MAX_PAGES + 1):
            ids = [i for i in ids_of(self.http_get(url.format(page=page)).decode("utf-8", "replace"))
                   if i not in seen]
            if not ids:
                break
            seen += ids
            time.sleep(PAUSE_S)
        return sorted(seen)

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        if backfill:
            todo = [(ARCHIVE_PDF.format(id=i), f"ffd_archive_{i}.pdf")
                    for i in self._listed(ARCHIVE_LIST, archive_ids)]
            todo += [(CURRENT_BODY.format(id=i), f"ffd_bulletin_{i}.html")
                     for i in self._listed(CURRENT_LIST, current_ids)]
        else:
            ids = current_ids(self.http_get(CURRENT_LIST.format(page=1)).decode("utf-8", "replace"))
            todo = [(CURRENT_BODY.format(id=i), f"ffd_bulletin_{i}.html") for i in ids[-LATEST_N:]]
            if not todo:
                # between seasons FFD may move the season's bulletins to the archive and list none
                page = self.http_get(ARCHIVE_LIST.format(page=1)).decode("utf-8", "replace")
                newest = list(dict.fromkeys(int(i) for i in re.findall(r"/bulletins/archive/(\d+)/download", page)))
                todo = [(ARCHIVE_PDF.format(id=i), f"ffd_archive_{i}.pdf") for i in newest[:LATEST_N]]
            if not todo:
                raise ValueError("no bulletins listed on the FFD bulletin or archive pages")
        out: list[FetchedFile] = []
        errors: list[Exception] = []
        for k, (url, fname) in enumerate(todo):
            if backfill and k:
                time.sleep(PAUSE_S)
            try:
                out.append(FetchedFile(filename=fname, content=self.http_get(url), when=date.today()))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
        if errors and not out:
            raise errors[0]
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        if f.content[:4] == b"%PDF":
            b = parse_pdf(f.content)
        else:
            b = parse_lines(html_lines(f.content.decode("utf-8", "replace")))
        if b is None:
            log.info("%s: %s has no gauge table", self.name, f.filename)
            return []
        if b["dropped"]:
            log.warning("%s: %s (%s): no reading for %s (cells not printed as flows)", self.name, f.filename,
                        b["date"], ", ".join(b["dropped"]))
        missing = [s for s in _SITES if s not in b["flows"] and s not in b["dropped"] and s != "Chiniot Bridge"]
        if missing:
            log.info("%s: %s (%s) has no row for %s", self.name, f.filename, b["date"], ", ".join(missing))
        return to_records(b)
