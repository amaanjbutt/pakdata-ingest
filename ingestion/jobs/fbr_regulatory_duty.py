"""fbr_regulatory_duty - regulatory duty on imports by PCT code, from FBR's annual RD notifications.

Source: the Federal Government levies regulatory duty (section 18(3) of the Customs Act) by an SRO
that replaces the previous one each budget, listed on https://www.fbr.gov.pk/ShowSROs?Department=Customs
(`POST /Home/LoadSROs`, the same DataTables form as the valuation rulings). Each is a table:
    S. No. | PCT Code | Description | Rate of regulatory duty (%)
with a code at 8 digits ("0101.2100") or for a whole heading ("03.07", "0307") or sub-heading
("0307.11"). `SROS` names, per fiscal year, the SRO in force on 1 July. Some are Word PDFs, read from
their text (`parse_pages`); the scans are read from `backend/data/customs/regulatory_duty_claude.json`,
Claude's transcription of the page images (`scripts/claude_sros/`): SRO 680(I)/2019 is a bare scan, and
the scanner's OCR of 966(I)/2022, 1152(I)/2025 and 1064(I)/2026 drops decimal points ("17.5" read as
"175", "8.0" as "80") and shifts rates between rows. Every page of those three was checked against the
OCR's digits; every row still has to pass the same checks as a parsed one.

Years that kept an SRO apply the amendments in force on 1 July (`AMENDMENTS`, from the same file): FY2020-21 =
680(I)/2019 + 127(I)/2020 + 573(I)/2020; FY2023-24 = 966(I)/2022 + 2293(I)/2022 + 775(I)/2023. An
amendment that says "for the figure X the figure Y" must find X in the table it amends, or the year fails.
Not applied: amendments that had lapsed by 1 July (949(I)/2019, undone by 38(I)/2020; 119(I)/2020, to
31 March 2020; 1571(I)/2022, 1633(I)/2022 and 1722(I)/2022, to 31 March 2023 by 204/205(I)/2023), and
conditions by importer, end use or origin (paragraphs 2-4 of each SRO: Fifth Schedule and Chapter 99
imports, CKD kits of home appliances, Afghan pine nuts …). FY2014-15 → FY2017-18 are not covered: RD then sat
in several SROs at once (568(I)/2014, 482(I)/2009 and single-item SROs for sugar, wheat, fuel, maize, yarn
and steel), each amended many times, and FBR hosts 568(I)/2014 only as an "updated" consolidation.
SRO 1065(I)/2026 (RD on commercial imports of used vehicles, on top of 1064) is not per code either.

A heading or sub-heading entry applies to every code of that year's tariff under it, except the codes
its description excepts ("except PCT code 0304.6200"); an 8-digit entry overrides its heading's. When
several entries of the same level cover a code with different rates (value bands of mobile phones,
ages of vehicles), the code keeps each rate with its description as text. An entry only counts if its
code is in that year's tariff (OCR misreads fall out). Rows: `customs_tariff_duties` component 'rd'
(source = the SRO that set the rate), one per code per fiscal year. FBR answers GitHub's runners →
`customs.yml`.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob
from ingestion.jobs.fbr_customs_tariff import fiscal_year, group_lines, pdf_words

S = "https://download1.fbr.gov.pk/SROs/"
# fiscal year (ending 30 June) -> (SRO, its PDF): the RD notification in force on 1 July of that year.
SROS: dict[int, tuple[str, str]] = {
    # FBR's "UP-DATED" copy of the May 2018 SRO, as amended by SRO 710(I)/2018 (June): in force on 1 July 2018
    2019: ("640(I)/2018", S + "20188171485248500UP-DATED-SRO-640(I)-2018.pdf"),
    2020: ("680(I)/2019", S + "20196291563424291SRO-680(I)-2019-28.06.2019.pdf"),
    2021: ("680(I)/2019", S + "20196291563424291SRO-680(I)-2019-28.06.2019.pdf"),
    2022: ("840(I)/2021", S + "202171872659187SRO840(I)2021,30.06.2021.pdf"),
    2023: ("966(I)/2022", S + "2022711173319584SRO966(I)2022.pdf"),
    2024: ("966(I)/2022", S + "2022711173319584SRO966(I)2022.pdf"),
    2025: ("928(I)/2024", S + "20246301562619990sro.928.pdf"),
    2026: ("1152(I)/2025", S + "202572975650649SRO.1152of2025.pdf"),
    2027: ("1064(I)/2026", S + "202663017637155381064-2026.pdf"),
}
# fiscal year -> the amending SROs in force on 1 July, in order, each with its clauses as operations on the
# table it amends: ("insert", after S. No.) and ("substitute", [S. Nos.]) take their rows from that SRO's own
# transcribed table; ("rate", [S. Nos.], from, to) checks `from` against the table; ("omit", [S. Nos.]).
AMENDMENTS: dict[int, list[tuple[str, list[tuple]]]] = {
    2021: [
        ("127(I)/2020", [("omit", ["86"])]),
        ("573(I)/2020", [
            ("insert", "82"), ("insert", "120"), ("insert", "130"), ("insert", "156"), ("insert", "169"),
            ("insert", "172"), ("substitute", ["185"]), ("rate", ["186", "187"], "8", "4"),
            ("rate", ["191", "192", "193", "194", "195"], "5", "2.5"), ("rate", ["230"], "10", "5"),
            ("omit", ["253"]), ("rate", ["259", "262", "264", "267"], "40", "20"), ("rate", ["266"], "20", "10"),
            ("rate", ["286"], "12.5", "6"), ("substitute", ["302"]), ("substitute", ["303"]), ("insert", "330"),
            ("rate", ["334"], "5", "10"), ("substitute", ["337"]), ("omit", ["343", "344", "345"]),
            ("substitute", ["348"]), ("substitute", ["349"]), ("substitute", ["430", "431"]),
            ("rate", ["442", "444"], "20", "10"), ("omit", ["443"]), ("rate", ["445", "446"], "15", "10"),
            ("insert", "529")]),
    ],
    2024: [
        ("2293(I)/2022", [("rate", ["139"], "20", "10"), ("insert", "209")]),
        ("775(I)/2023", [
            ("insert", "140"), ("insert", "168"), ("insert", "297"), ("rate", ["299"], "15", "30"),
            ("substitute", ["300"]), ("substitute", ["306"]), ("insert", "308"), ("insert", "492"),
            ("insert", "499")]),
    ],
}
CLAUDE_FILE = "data/customs/regulatory_duty_claude.json"
_CODEISH = re.compile(r"^[0-9OoIl'’.,]{2,12}$")
_RATE = re.compile(r"^\d{1,3}(?:\.\d+)?%?$")
_SNO = re.compile(r"^\d{1,4}[.)]?$")


@dataclass
class RdEntry:
    entry: str          # the code as printed, digits and dots ('03.07', '0101.2100', '0307.11')
    description: str
    rate_pct: float | None
    rate_text: str | None
    sro: str | None = None   # the amending SRO that set this entry (None: the year's own SRO)


def parse_rate(cell: str | None) -> tuple[float | None, str | None]:
    """A rate cell as printed -> (percent, None) or (None, the text). Pure."""
    t = " ".join(str(cell or "").split())
    if not t:
        return None, None
    if _RATE.match(t):
        return float(t.rstrip("%")), None
    return None, t


def norm_code(tokens: list[str]) -> str | None:
    """OCR'd code fragments of one cell -> 'NN.NN' / 'NNNN.NN' / 'NNNN.NNNN', or None. Pure."""
    t = "".join(tokens).translate(str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "'": "", "’": "", ",": ""}))
    digits = re.sub(r"\D", "", t)
    if len(digits) == 4:
        return f"{digits[:2]}.{digits[2:]}"
    if len(digits) == 6:
        return f"{digits[:4]}.{digits[4:]}"
    if len(digits) == 8:
        return f"{digits[:4]}.{digits[4:]}"
    return None


def _mode(xs: list[float]) -> float | None:
    return Counter(round(x / 5) * 5 for x in xs).most_common(1)[0][0] if xs else None


def parse_pages(pages: list[list[tuple]]) -> list[RdEntry]:
    """Word boxes -> one RdEntry per code cell. Pure."""
    out: list[RdEntry] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur and cur["codes"]:
            rate = " ".join(cur["rate"]).strip()
            pct = float(rate.rstrip("%")) if _RATE.match(rate) else None
            for c in cur["codes"]:
                out.append(RdEntry(c, " ".join(cur["desc"]).strip(), pct, None if pct is not None else (rate or None)))
        cur = None

    for words in pages:
        lines = [sorted(ln) for ln in group_lines(words, tol=4)]
        # the code column: where tokens that read as codes cluster, left of the text
        cands = [w[0] for ln in lines for w in ln[:3] if _CODEISH.match(w[3]) and norm_code([w[3]])]
        code_x = _mode(cands)
        rates = [w[0] for ln in lines for w in ln if _RATE.match(w[3]) and code_x is not None and w[0] > code_x + 200]
        rate_x = _mode(rates)
        if code_x is None or rate_x is None:
            continue
        for ln in lines:
            sno = [w for w in ln if w[0] < code_x - 15 and _SNO.match(w[3])]
            code_w = [w for w in ln if code_x - 15 <= w[0] < code_x + 45 and _CODEISH.match(w[3])]
            rate_w = [w for w in ln if w[0] >= rate_x - 20]
            desc_w = [w for w in ln if code_x + 45 <= w[0] < rate_x - 20 or (code_x - 15 <= w[0] < code_x + 45 and not _CODEISH.match(w[3]))]
            # a new row: its S. No., or (the OCR lost the S. No.) a code and a rate on one line
            # after a row that already has both
            if sno or (code_w and rate_w and cur and cur["codes"] and cur["rate"]):
                flush()
                cur = {"codes": [], "desc": [], "rate": []}
            if cur is None:
                continue
            code = norm_code([w[3] for w in code_w]) if code_w else None
            if code:
                cur["codes"].append(code)
            cur["desc"] += [w[3] for w in desc_w]
            if rate_w and not cur["rate"]:
                cur["rate"] = [w[3] for w in rate_w]
    flush()
    return out


_EXCEPT = re.compile(r"\b(?:except|excluding)\b(.*)", re.I | re.S)
_CODE_IN_TEXT = re.compile(r"\b(\d{4}\.\d{2}(?:\d{2})?|\d{2}\.\d{2})\b")
_ENTRY = re.compile(r"\d{2}\.\d{2}|\d{4}(?:\.\d{2}(?:\d{2})?)?")


def excepted(description: str) -> list[str]:
    """The codes an entry's description excepts ("… (PCT code except 0304.6200)"), as digit prefixes. Pure."""
    m = _EXCEPT.search(description or "")
    return [c.replace(".", "") for c in _CODE_IN_TEXT.findall(m.group(1))] if m else []


def expand(entries: list[RdEntry], codes: set[str]) -> dict[str, RdEntry]:
    """Entries -> {8-digit code: entry} over a year's tariff codes: a heading or sub-heading entry
    covers its codes but the ones its description excepts, an 8-digit entry overrides it. Several
    entries of one level with different rates (value bands, ages) -> one entry whose text carries
    each rate with its description. Entries whose code isn't in the tariff (or covers nothing) and
    entries without a rate are dropped. Pure."""
    found: dict[str, tuple[int, list[RdEntry]]] = {}
    for e in entries:
        if e.rate_pct is None and not e.rate_text:
            continue
        d = e.entry.replace(".", "")
        if len(d) < 8:
            skip = [x for x in excepted(e.description) if x.startswith(d) and x != d]
            hits = [c for c in codes if c.startswith(d) and not any(c.startswith(x) for x in skip)]
        else:
            hits = [d] if d in codes else []
        for c in hits:
            level, have = found.get(c, (-1, []))
            if len(d) > level:
                found[c] = (len(d), [e])
            elif len(d) == level:
                have.append(e)
    out: dict[str, RdEntry] = {}
    for c, (_, es) in found.items():
        if len({(e.rate_pct, e.rate_text) for e in es}) == 1:
            out[c] = es[-1]
        else:
            text = "; ".join(f"{_shown(e)} ({_short(e.description)})" for e in es)
            out[c] = RdEntry(es[0].entry, text, None, text, next((e.sro for e in es if e.sro), None))
    return out


def _shown(e: RdEntry) -> str:
    return e.rate_text or f"{e.rate_pct:g}%"


def _short(t: str, n: int = 90) -> str:
    t = " ".join(t.split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


# ---- Claude's transcription of the scanned SROs (data/customs/regulatory_duty_claude.json) ------------

def load_claude(path: str | None = None) -> dict[str, dict]:
    """{SRO: transcription} from the repo's data file ({} when absent)."""
    p = path or os.path.join(os.path.dirname(__file__), "..", "..", CLAUDE_FILE)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)["sros"]


def _same_rate(a: str | None, b: str) -> bool:
    pa, ta = parse_rate(a)
    pb, tb = parse_rate(b)
    return pa == pb if pa is not None and pb is not None else (ta or "").lower() == (tb or "").lower()


def amended_table(base: dict, amendments: list[tuple[str, list[tuple]]], sros: dict[str, dict]) -> list[dict]:
    """The base SRO's table rows with each amending SRO's clauses applied in order. Every row carries
    `sro` (the amending SRO that set it, or None). Raises when a clause names an S. No. the table
    lacks, or its "for the figure X" doesn't match the table. Pure."""
    rows = [dict(r, sro=None) for r in base["rows"]]

    def at(sno: str, sro: str) -> int:
        for i, r in enumerate(rows):
            if r.get("sno") == sno:
                return i
        raise ValueError(f"{sro}: S. No. {sno} is not in the table it amends")

    for sro, ops in amendments:
        new = sros[sro]["rows"]
        for op in ops:
            kind = op[0]
            if kind == "insert":
                add = [dict(r, sro=sro) for r in new if r.get("inserted_after") == op[1]]
                if not add:
                    raise ValueError(f"{sro}: no rows transcribed for the insertion after S. No. {op[1]}")
                i = at(op[1], sro) + 1
                while i < len(rows) and re.fullmatch(rf"{re.escape(op[1])}[A-Z]+", rows[i].get("sno") or ""):
                    i += 1   # after the rows an earlier amendment inserted there
                rows[i:i] = add
            elif kind == "substitute":
                add = [dict(r, sro=sro) for r in new if r.get("substituted_for") in op[1]]
                if not add:
                    raise ValueError(f"{sro}: no rows transcribed to substitute S. No. {op[1]}")
                i = at(op[1][0], sro)
                for sno in op[1]:
                    del rows[at(sno, sro)]
                rows[i:i] = add
            elif kind == "rate":
                for sno in op[1]:
                    r = rows[at(sno, sro)]
                    if not _same_rate(r.get("rate"), op[2]):
                        raise ValueError(f"{sro}: S. No. {sno} reads {r.get('rate')!r}, the amendment says {op[2]!r}")
                    r["rate"], r["sro"] = op[3], sro
            elif kind == "omit":
                for sno in op[1]:
                    del rows[at(sno, sro)]
            else:
                raise ValueError(f"{sro}: unknown operation {kind!r}")
    return rows


def claude_entries(fy: int, sros: dict[str, dict] | None = None) -> list[RdEntry] | None:
    """The year's entries from Claude's transcription, amendments applied (None when the year's SRO
    isn't in the file). Pure given `sros`."""
    sros = load_claude() if sros is None else sros
    base = sros.get(SROS[fy][0])
    if base is None:
        return None
    out: list[RdEntry] = []
    for r in amended_table(base, AMENDMENTS.get(fy, []), sros):
        pct, text = parse_rate(r.get("rate"))
        for c in r.get("codes") or []:
            if _ENTRY.fullmatch(c):
                out.append(RdEntry(c, " ".join(str(r.get("description") or "").split()), pct, text, r.get("sro")))
    return out


@dataclass
class RdRow:
    fy: int
    code: str
    rate_pct: float | None
    rate_text: str | None
    source: str


class FbrRegulatoryDutyJob(IngestionJob):
    name = "fbr_regulatory_duty"
    source = "FBR"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        years = sorted(SROS) if backfill else sorted(SROS)[-1:]
        return [FetchedFile(f"{fy}", self.http_get(SROS[fy][1]), date.today()) for fy in years]

    def parse(self, f: FetchedFile):
        from app import db

        fy = int(f.filename)
        codes = {r["code"] for r in db.query("SELECT code FROM customs_tariff WHERE fy = %s", (fy,))}
        if not codes:
            raise ValueError(f"FY{fiscal_year(fy)}: no tariff edition loaded to check the RD codes against")
        entries = claude_entries(fy)
        if entries is None:
            entries = parse_pages(list(pdf_words(f.content)))
        rows = expand(entries, codes)
        return [RdRow(fy, c, e.rate_pct, e.rate_text, f"SRO {e.sro or SROS[fy][0]}") for c, e in sorted(rows.items())]

    def validate(self, records):
        for fy, n in Counter(r.fy for r in records).items():
            if n < 100:   # every RD SRO covers several hundred codes
                raise ValueError(f"FY{fiscal_year(fy)}: only {n} RD codes parsed")
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        from app import db

        rows = [r for r, _ in validated]
        n = 0
        with db.connection() as conn, conn.cursor() as cur:
            for fy in sorted({r.fy for r in rows}):
                mine = [r for r in rows if r.fy == fy]
                cur.executemany(
                    """INSERT INTO customs_tariff_duties (code, fy, component, rate_pct, rate_text, source)
                       VALUES (%s, %s, 'rd', %s, %s, %s)
                       ON CONFLICT (code, fy, component) DO UPDATE SET rate_pct = EXCLUDED.rate_pct,
                           rate_text = EXCLUDED.rate_text, source = EXCLUDED.source
                       WHERE (customs_tariff_duties.rate_pct, customs_tariff_duties.rate_text, customs_tariff_duties.source)
                             IS DISTINCT FROM (EXCLUDED.rate_pct, EXCLUDED.rate_text, EXCLUDED.source)""",
                    [(r.code, r.fy, r.rate_pct, r.rate_text, r.source) for r in mine])
                n += cur.rowcount
                cur.execute("DELETE FROM customs_tariff_duties WHERE fy = %s AND component = 'rd' AND NOT (code = ANY(%s))",
                            (fy, [r.code for r in mine]))
        return n
