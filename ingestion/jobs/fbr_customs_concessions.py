"""fbr_customs_concessions - the Fifth Schedule to the Customs Act: concessionary customs duty by PCT code, FY2016-17 →.

Source: FBR's Fifth Schedule to the Customs Act, 1969 (section 18(1A)) — the concessions on customs
duty for machinery, inputs, pharmaceuticals, EVs and the rest, one edition a year beside the tariff:
    https://www.fbr.gov.pk/categ/laws/144 and …/categ/customs-tariff/51149/70853/131190 (previous years);
    FY2022-23 from FBR's Budget 2022-23 site. `EDITIONS` lists every file read.
Each edition is a Word document of Parts (I: plant and machinery; II: pharmaceutical raw materials;
III: raw materials and inputs; IV–VIII: …), each a table whose columns move from Part to Part:
    S. No. | Description | PCT Code | Customs Duty (%) | Conditions
A serial holds lettered / numbered sub-items ("A) Tillage …", "(1). Tractor mounted trencher"); a
sub-item's rate is printed on its first line and covers every code listed under it, one per line.

Columns are found per page from where the codes and rates actually sit (the mode of their x), not
from the header, which only some pages repeat; a page where no code sits beside a rate (a serial's one
"0%" printed on its code-less first line) takes the rate column of the page before, and a page of one
serial's numbered sub-items ("1. Pre-fabricated …" … "47. Parachutes", indented right of the S. No.
column) keeps the S. No. column of the page before. A rate printed for a lettered sub-item ("(b). Raw
materials … 0%") covers the (i), (ii) … under it; a rate phrase printed down the column beside the
first sub-items ("50% of the / prevailing / rate of customs duty") is the serial's, whole. One row per (part, serial, sub-item, code): codes as
printed — headings ("8418"), "Respective headings" and chapter entries carry no 8-digit code and are
counted, not stored. A rate cell is kept as printed when it isn't a plain percentage ("0%, 3%, 11%,
15%" for "Respective headings" entries). Rows go to `customs_concessions`; FBR answers GitHub's
runners → `customs.yml` with the tariff.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob
from ingestion.jobs.fbr_customs_tariff import D1, fiscal_year, group_lines, pdf_words

EDITIONS: dict[int, str] = {
    2017: D1 + "2016102015102621114FifthSchedule.pdf",
    2018: D1 + "20171121121122278FifthScheduletotheCustomsAct.pdf",
    2019: D1 + "2019491441258797FifthScheduleupdated.pdf",
    2020: D1 + "2019629166146700SecondSheduletotheFinanceBill(FifthScheduletotheCustomsAct1969).pdf",
    2021: D1 + "202011613115025617FifthSchedule.pdf",
    2022: D1 + "2021711171234418SecondScheduleofFA(5thScheduleofCA).pdf",
    2023: "https://www.fbr.gov.pk/Budget2022-23/CustomsSchedulesFinal/Fifth-Schedule-to-the-Customs-Act-1969.pdf",
    2024: D1 + "2023111715113244746FifthSchedule-23-24.pdf",
    2025: D1 + "2025328113425842Fifth-Schedule-24..pdf",
    2026: D1 + "202585238495687Fifth-Schedule.pdf",
    2027: D1 + "2026825118858478FifthSchedule2026-27.pdf",
}
_CODE = re.compile(r"^\d{4}\.\d{4}$")
_HEAD_CODE = re.compile(r"^\d{4}(?:\.\d{2})?$|^\d{2}\.\d{2}$")
_RATE = re.compile(r"^(?:\d{1,3}(?:\.\d+)?%?|Nil|Free|Exempt)[,;]?$", re.I)
_SERIAL = re.compile(r"^\d{1,3}[A-Z]?\.?$")
_SUB = re.compile(r"^(?:\(?[A-Za-z]\)|\(?\d{1,3}\)\.?|\(\d{1,3}\)\.?|\([ivxlc]{1,6}\)|[ivx]{1,4}\))", re.I)
_GROUP = re.compile(r"^\(?(?![ivxIVX][).])[A-Za-z]\)\.?")   # a lettered sub-item, not a roman (i)/(v)/(x)
_PART = re.compile(r"^Part[\s-]*([IVX]+(?:\s*\([A-Z]\))?)\b", re.I)


@dataclass
class ConcessionLine:
    fy: int
    part: str          # 'I', 'II', 'V(A)' …
    serial: str        # '1', '15A'
    sub_item: str      # '' or the sub-item's label + text ("A) Tillage …> (1). Tractor mounted trencher")
    code: str          # 8 digits
    description: str   # the serial's and sub-item's text
    rate_pct: float | None
    rate_text: str | None
    condition: str | None
    source_url: str = ""


def parse_rate(cells: list[str]) -> tuple[float | None, str | None]:
    t = " ".join(cells).strip().rstrip(",;")
    if not t:
        return None, None
    if len(set(t.split())) == 1:   # the same rate printed against each code of a sub-item ("0% 0%")
        t = t.split()[0]
    if re.fullmatch(r"\d{1,3}(?:\.\d+)?%?", t):
        return float(t.rstrip("%")), None
    if re.fullmatch(r"(?i)nil|free|exempt", t):
        return 0.0, t
    return None, t


def _mode_x(xs: list[float]) -> float | None:
    if not xs:
        return None
    return Counter(round(x / 4) * 4 for x in xs).most_common(1)[0][0]


@dataclass
class _Entry:
    part: str
    serial: str
    text: list
    sub: str = ""
    n: int = 0                    # sub-items seen so far (labels repeat: "(1)." under A), C), …)
    rates: dict | None = None     # sub-item number ('' = the serial itself) -> rate cells printed for it
    cond: list | None = None
    bare: int = 0                 # bare-numbered sub-items seen ("1 Pre-fabricated …", "2 …" under one serial)
    group: str = ""               # the lettered sub-item ("(b). Raw materials …") the current items sit under
    top: str = ""                 # the numbered sub-item ("8. Following machinery …") they sit under


_COLNUMS = re.compile(r"^(?:\(\d\)\s*)+$")


def parse_pages(pages: list[list[tuple]], fy: int, source_url: str = "") -> tuple[list[ConcessionLine], Counter]:
    """Word boxes per page -> one ConcessionLine per code. Pure. Also counts what isn't stored.

    A code takes the rate printed for its own sub-item; failing that, the serial's own rate, or the
    serial's only rate when every sub-item shares one. A serial whose one rate is a phrase printed
    down the rate column beside its first sub-items ("50% of the / prevailing / rate of / customs /
    duty") gets the phrase whole, for every code."""
    out: list[ConcessionLine] = []
    skipped: Counter = Counter()
    part = ""
    entry: _Entry | None = None
    pending: list[tuple] = []   # (entry, sub-item label, sub-item text, code)
    prev_cols: tuple[float, float] | None = None   # (code_x, rate_x) of the last page that showed both
    prev_serial: float | None = None   # the S. No. column of the page before, in this Part

    def flush():
        nonlocal pending
        for e, key, sub, code, group, top, own in pending:
            rates = e.rates or {}
            frags = [c for v in rates.values() for c in v]
            if len(rates) > 1 and frags and all(parse_rate(v)[0] is None for v in rates.values() if v) \
                    and parse_rate(frags)[1] and re.match(r"\d", frags[0]) and not re.search(r"%\S*$", frags[-1]):
                cells = frags          # one phrase broken over the sub-items' lines
            elif own and len(set(rates.get(key) or [])) > 1:
                cells = own            # codes of one sub-item, each with its own rate on its line
            else:
                cells = rates.get(key) or rates.get(group) or rates.get(top) or rates.get("")
            if not cells:
                distinct = {" ".join(v) for v in rates.values() if v}
                cells = [next(iter(distinct))] if len(distinct) == 1 else []
            conds = list(e.cond or [])
            if not cells and not rates and conds and _RATE.match(conds[0]):
                cells = [conds.pop(0)]   # the serial's rate printed past the rate column ("Nil If imported …")
            pct, txt = parse_rate(cells)
            if pct is None and txt is None:
                skipped["no rate printed"] += 1
            desc = " ".join(e.text).strip()
            cond = " ".join(conds).strip() or None
            out.append(ConcessionLine(fy, e.part, e.serial, sub, code, desc, pct, txt, cond, source_url))
        pending = []

    def read_line(ln: list, code_x: float, code_end: float, rate_x: float | None, cond_x: float,
                  serial_x: float | None) -> None:
        nonlocal entry
        text = " ".join(w[3] for w in ln)
        if _COLNUMS.match(text) or ("PCT" in text and ("Description" in text or "Duty" in text)):
            return   # the table's header or its column numbers
        desc = [w for w in ln if w[0] < code_x - 6]
        codes = [w for w in ln if code_x - 6 <= w[0] < code_end
                 or (code_end <= w[0] < cond_x and _CODE.match(w[3].rstrip(",;")))]
        # the rate cell: the rate column, and left of it only a phrase ("50% of the prevailing …"; a
        # lone "3%,5%" there belongs to a "Respective headings" entry, which isn't stored)
        cell = [w for w in ln if rate_x is not None and code_end <= w[0] < cond_x and not _CODE.match(w[3].rstrip(",;"))]
        phrase = any(re.search(r"[A-Za-z]{2}", w[3]) and not _RATE.match(w[3]) for w in cell)
        rates = [w for w in cell if phrase or w[0] >= rate_x - 6]
        conds = [w for w in ln if w[0] >= cond_x]
        first = desc[0] if desc else None
        if first is not None and _SERIAL.match(first[3]) and serial_x is not None and abs(first[0] - serial_x) <= 8:
            flush()
            entry = _Entry(part, first[3].rstrip("."), [w[3] for w in desc[1:]], rates={}, cond=[])
            desc = []
        elif entry is None:
            return
        if desc:
            t = " ".join(w[3] for w in desc)
            # a bare number right of the S. No. column, continuing its run ("1", "2" … — the schedule
            # sometimes skips or repeats one): a sub-item
            num = desc[0][3].rstrip(".")
            bare = (_SERIAL.match(desc[0][3]) is not None and serial_x is not None and desc[0][0] > serial_x + 15
                    and num.isdigit() and (int(num) == 1 if not entry.bare else entry.bare <= int(num) <= entry.bare + 3))
            if _SUB.match(t) or bare:
                entry.sub = t
                entry.n += 1
                if bare:              # "8. Following machinery …": its rate covers what is listed under it
                    entry.bare, entry.top, entry.group = int(num), str(entry.n), ""
                elif _GROUP.match(t):   # "(b). Raw materials …": likewise for the (i), (ii) … below it
                    entry.group = str(entry.n)
            elif entry.sub:
                entry.sub = f"{entry.sub} {t}"
            else:
                entry.text.append(t)
        if rates:
            entry.rates.setdefault(str(entry.n) if entry.sub else "", []).extend(w[3] for w in rates)
        if conds:
            entry.cond.extend(w[3] for w in conds)
        for w in codes:
            tok = w[3].rstrip(",;")
            if _CODE.match(tok):
                pending.append((entry, str(entry.n) if entry.sub else "", entry.sub, tok.replace(".", ""), entry.group,
                                entry.top, [w[3] for w in rates]))
            elif _HEAD_CODE.match(tok) or tok.lower().startswith(("respective", "ch.", "chapter")):
                skipped["heading or chapter entry"] += 1

    def pieces(words: list[tuple]):
        """A page's lines, split where a Part heading sits (each Part has its own columns: Part IV of
        FY2017-18 has no description column); a heading comes through as its Part name."""
        cur: list = []
        for ln in (sorted(ln) for ln in group_lines(words)):
            m = _PART.match(" ".join(w[3] for w in ln))
            if m and len(ln) <= 14:
                yield cur
                yield m.group(1).replace(" ", "").upper()
                cur = []
            else:
                cur.append(ln)
        yield cur

    for words in pages:
        for lines in pieces(words):
            if isinstance(lines, str):
                flush()
                part, entry, prev_serial = lines, None, None
                continue
            code_x = _mode_x([w[0] for ln in lines for w in ln if _CODE.match(w[3])])
            if code_x is None:
                continue   # notes / conditions: no table rows
            # the rate column: rate-like tokens on the lines that carry a code (the notes above a table
            # are full of "0%, 3% or 11%" too)
            coded = [ln for ln in lines if any(_CODE.match(w[3]) and abs(w[0] - code_x) <= 12 for w in ln)]
            rate_x = _mode_x([w[0] for ln in coded for w in ln if w[0] > code_x + 30 and _RATE.match(w[3])])
            if rate_x is None and prev_cols and abs(prev_cols[0] - code_x) <= 12:
                # no code here sits beside a rate (a serial's one rate is printed on its first,
                # code-less line): the columns of the page before
                rate_x = prev_cols[1]
            if rate_x is not None:
                prev_cols = (code_x, rate_x)
            cond_x = (rate_x + 30) if rate_x is not None else code_x + 60
            # codes sit in a column about one code wide; what follows, up to the condition column, is
            # the rate cell ("50% of the prevailing rate of customs duty" starts left of the "0%" cells)
            code_end = min(rate_x - 6, code_x + 52) if rate_x is not None else code_x + 60
            # the S. No. column: where line-initial numbers sit (a wrapped "150 meters)" is not a
            # serial). A serial's numbered sub-items ("1. Pre-fabricated …" … "47. Parachutes") sit right
            # of it: where they outnumber the serials, the serials' column wins, and on a page of
            # sub-items only, the column of the page before (in the same Part) stands.
            xs = [ln[0][0] for ln in lines if _SERIAL.match(ln[0][3]) and ln[0][0] < code_x - 6]
            serial_x = _mode_x(xs)
            left = [x for x in xs if serial_x is not None and x < serial_x - 15]
            if len(left) >= 2:
                serial_x = _mode_x(left)
            if prev_serial is not None and (serial_x is None or serial_x > prev_serial + 15):
                serial_x = prev_serial
            if serial_x is not None:
                prev_serial = serial_x
            for ln in lines:
                read_line(ln, code_x, code_end, rate_x, cond_x, serial_x)
    flush()
    return out, skipped


def parse_pdf(content: bytes, fy: int, source_url: str = "") -> tuple[list[ConcessionLine], Counter]:
    return parse_pages(list(pdf_words(content)), fy, source_url)


def dedupe(rows: list[ConcessionLine]) -> list[ConcessionLine]:
    """One row per (fy, part, serial, sub_item, code): a code listed twice under one sub-item keeps
    its first printing. Pure."""
    seen: dict[tuple, ConcessionLine] = {}
    for r in rows:
        seen.setdefault((r.fy, r.part, r.serial, r.sub_item, r.code), r)
    return list(seen.values())


class FbrCustomsConcessionsJob(IngestionJob):
    name = "fbr_customs_concessions"
    source = "FBR"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        years = sorted(EDITIONS) if backfill else sorted(EDITIONS)[-2:]
        return [FetchedFile(f"{fy}|{EDITIONS[fy]}", self.http_get(EDITIONS[fy]), date.today()) for fy in years]

    def parse(self, f: FetchedFile):
        fy, url = f.filename.split("|", 1)
        rows, _ = parse_pdf(f.content, int(fy), url)
        return rows

    def validate(self, records):
        from app import db

        rows = dedupe(records)
        known = {(r["code"], r["fy"]) for r in db.query("SELECT code, fy FROM customs_tariff")}
        by_fy: Counter = Counter(r.fy for r in rows)
        for fy, n in by_fy.items():
            if n < 500:  # every edition lists a few thousand coded concessions
                raise ValueError(f"FY{fiscal_year(fy)}: only {n} concession rows parsed")
        # a code that isn't in that year's tariff is a misread (or FBR's own typo): kept out
        good = [r for r in rows if not known or (r.code, r.fy) in known]
        self.last_rejects = [f"{r.fy} {r.code}" for r in rows if known and (r.code, r.fy) not in known]
        return [(r, False) for r in good]

    def upsert(self, validated) -> int:
        return upsert_rows([r for r, _ in validated])


def upsert_rows(rows: list[ConcessionLine]) -> int:
    """Replace each edition's rows as a whole (an edition is fixed once published; a re-read that
    differs replaces it), writing nothing when an edition is unchanged."""
    from app import db

    n = 0
    with db.connection() as conn, conn.cursor() as cur:
        for fy in sorted({r.fy for r in rows}):
            mine = [r for r in rows if r.fy == fy]
            cur.execute("""SELECT part, serial, sub_item, code, description, rate_pct, rate_text, condition
                           FROM customs_concessions WHERE fy = %s""", (fy,))
            have = {tuple(x[:4]): tuple(x[4:]) for x in cur.fetchall()}
            want = {(r.part, r.serial, r.sub_item, r.code): (r.description, r.rate_pct, r.rate_text, r.condition)
                    for r in mine}
            if {k: (v[0], None if v[1] is None else float(v[1]), v[2], v[3]) for k, v in have.items()} == want:
                continue
            cur.execute("DELETE FROM customs_concessions WHERE fy = %s", (fy,))
            cur.executemany(
                """INSERT INTO customs_concessions (fy, part, serial, sub_item, code, description, rate_pct, rate_text,
                       condition, source_url) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(r.fy, r.part, r.serial, r.sub_item, r.code, r.description, r.rate_pct, r.rate_text, r.condition,
                  r.source_url) for r in mine])
            n += len(mine)
    return n
