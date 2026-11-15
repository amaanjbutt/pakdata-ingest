"""Build the regulatory duty in force on 1 July 2014 → 1 July 2017 (FY2014-15 → FY2017-18) into
data/customs/regulatory_duty_fy2015_2018.json, which `fbr_regulatory_duty` reads for those years.

    python -m scripts.claude_sros.build_rd_fy2015_2018 TEXT_DIR DEEPSEEK_DIR

TEXT_DIR holds FBR's own files (text layers / Word) of the SROs, DEEPSEEK_DIR the transcriptions of the scanned
ones (claude_sros format, `sNNN.json`); both from the 2026-11 work (the owner's PC, `local/customs-text/` and
`local/rulings-deepseek/sros/out/` in that worktree; the originals are FBR's `download1.fbr.gov.pk/SROs/` files,
listed by the Internet Archive's index of that folder).

RD then sat in several SROs at once. 1035(I)/2017 (16 Oct 2017) superseded exactly these: 482(I)/2009, 808(I)/2009,
214(I)/2010, 568(I)/2014, 1043(I)/2014, 254(I)/2015, 393(I)/2015 and 1248(I)/2015 — so they are the RD SROs in force
until then, each from its own date. 392(I)/2015 (petroleum) isn't in that list: it had ended by then, when is not
known, so it counts only on 1 July 2015. Export RDs (potatoes 340(I)/2014, lead 594(I)/2009, pulses 492(I)/2006,
HSD/SKO to Afghanistan 946(I)/2008) are not import duties per code and are left out.

568(I)/2014 per year, from FBR's consolidated copies and the amendments dated before each 1 July:
  FY2015  the Jul 2014 consolidation minus 631(I)/2014 (11 Jul 2014: its four inserted rows)
  FY2016  the Mar 2015 consolidation (as amended to 246(I)/2015, 27 Mar) + 601(I)/2015 (inserts) and 602(I)/2015
          (omits S.No.280), both in force from 1 Jul 2015
  FY2017  the Mar 2016 consolidation (Table substituted by 1177(I)/2015, then 236(I)/2016); checked row by row
          against the transcription of 1177(I)/2015 with 236(I)/2016's changes
  FY2018  the Table substituted by 505(I)/2017 (21 Jun 2017) + 555(I)/2017 (1 Jul 2017)
482(I)/2009: the Aug 2013 consolidation; + 1190(I)/2015 and 1247(I)/2015 (Dec 2015) and 485(I)/2016 (from 1 Jul
2016) for FY2017; the Table substituted by 504(I)/2017 + 556(I)/2017 for FY2018.
214(I)/2010: no copy survives; the 2013-14 Customs Tariff prints RD 5% on 3906.9030 and 3906.9040, the only RD
codes there that 482(I)/2009 and 808(I)/2009 don't explain.
254(I)/2015 (wheat 25%): 854(I)/2016 (Sep 2016) sets S.No.1 "for 40, 60", so an amendment between Mar 2015 and Sep
2016 that isn't on file raised it to 40: FY2017 leaves 254 out, FY2018 has S.No.1 at 60 only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import zipfile

OUT = os.path.join(os.path.dirname(__file__), "..", "..", "data", "customs", "regulatory_duty_fy2015_2018.json")
CODE = r"(?:\d{2}\.\d{2}(?:\.\d{2,4})?|\d{4}\.\d{2,4})"
_SNO = re.compile(rf"(?<![\d.\w])(\d{{1,3}}\.|\d{{1,3}}[A-Z]{{1,3}}\s?\.?)\s+(?=\.?\s*{CODE}\b)")
_BARE = re.compile(r"(?<![\w.])(\d{1,2}(?:\.\d{1,2})?)(?![\w.%])")
_ADVAL = re.compile(r"\d+(?:\.\d+)?\s*%\s*ad[\s.-]*val(?:orem)?\.?", re.I)
_LEAD_CODES = re.compile(rf"^\.?\s*({CODE}(?:\s*(?:,|and|&|/)\s*{CODE})*)\s+")
_RATE_END = re.compile(r"\s(\d+(?:\.\d+)?\s*%?(?:\s*ad[\s.-]*val(?:orem)?\.?)?|\d[\d,]*\s*rupees\s+per\s+\w+|Rs\.?\s*[\d,]+\s*per\s+\w+)\s*$",
                       re.I)


def docx_rows(path: str) -> list[dict]:
    """A Word consolidation's table -> [{codes, description, rate}] (its S. Nos are Word numbering, not text)."""
    x = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8", "replace")
    out = []
    for tr in re.findall(r"<w:tr[ >].*?</w:tr>", x, flags=re.S):
        cells = []
        for tc in re.findall(r"<w:tc[ >].*?</w:tc>", tr, flags=re.S):
            paras = [re.sub(r"<[^>]+>", "", p) for p in re.findall(r"<w:p[ >].*?</w:p>", tc, flags=re.S)]
            cells.append(" ".join(" ".join(p.split()) for p in paras if p.strip()))
        if len(cells) == 4 and re.fullmatch(CODE, cells[1] or ""):
            out.append({"codes": [cells[1]], "description": cells[2], "rate": clean_rate(cells[3])})
    return out


def clean_rate(cell: str) -> str:
    """'10[30]' (footnote 10, the rate substituted to 30) -> '30'; '5]' -> '5'. Pure."""
    m = re.search(r"\[([\d.]+)\]?\s*$", cell)
    return m.group(1) if m else cell.replace("[", "").replace("]", "").strip()


def pdf_text(path: str) -> str:
    import pymupdf

    return " ".join("".join(p.get_text() for p in pymupdf.open(path)).split())


def numbered_rows(text: str) -> list[dict]:
    """'1. 0403.1000 Yogurt 10 2. 0403.9000 Other 10 …' -> [{sno, codes, description, rate}]. Footnote markers
    ('4[0802.8000 …]', '5[36A.') are dropped first. Pure."""
    t = re.sub(r"(?<![\d.])\d{1,2}(?:,\d{1,2})*,?\[", "", text)
    t = t.replace("[", " ").replace("]", " ")
    t = re.split(r"\bAs amended\b|\[C\.\s*No\.|\s2\.\s+(?:The\s+)?regulatory duty shall not", t, flags=re.I)[0]
    marks = list(_SNO.finditer(t))
    out = []
    for i, m in enumerate(marks):
        body = t[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(t)].strip()
        c = _LEAD_CODES.match(body)
        if not c:
            continue
        rest = " " + body[c.end():]
        r = _RATE_END.search(rest)
        if r:
            rate, desc = r.group(1), rest[: r.start()]
        else:   # the text layer can put the rate mid-description ("above 2000 50% ad valorem cc (…)",
            # "(including medicated 10 products)", "of 12.5 cast iron.")
            a = _ADVAL.search(rest) or (list(_BARE.finditer(rest)) or [None])[-1]
            if not a:
                continue
            rate, desc = a.group(0), rest[: a.start()] + " " + rest[a.end():]
        out.append({"sno": m.group(1).replace(" ", "").rstrip("."), "codes": re.findall(CODE, c.group(1)),
                    "description": " ".join(desc.split()), "rate": " ".join(rate.split())})
    return out


def ds(deepseek: str, sid: str) -> dict:
    with open(os.path.join(deepseek, f"{sid}.json"), encoding="utf-8") as f:
        return json.load(f)


def _at(rows: list[dict], sno: str) -> int:
    for i, r in enumerate(rows):
        if r.get("sno") == sno:
            return i
    raise ValueError(f"S. No. {sno} not in the table")


def substitute(rows: list[dict], snos: list[str], new: list[dict], sro: str) -> None:
    i = _at(rows, snos[0])
    for s in snos:
        del rows[_at(rows, s)]
    rows[i:i] = [dict(r, sro=sro) for r in new]


def omit(rows: list[dict], snos: list[str]) -> None:
    for s in snos:
        del rows[_at(rows, s)]


def snos_like(rows: list[dict], *bases: str) -> list[dict]:
    """The amendment's rows whose S. No. is one of `bases` or a lettered one after it (38 -> 38, 38A …)."""
    return [r for r in rows if any(re.fullmatch(rf"{b}[A-Z]*", r.get("sno") or "") for b in bases)]


def _row(sno: str, code: str, desc: str, rate: str, sro: str) -> dict:
    return {"sno": sno, "codes": [code], "description": desc, "rate": rate, "sro": sro}


def insert_after(rows: list[dict], sno: str, new: list[dict]) -> None:
    i = _at(rows, sno) + 1
    while i < len(rows) and re.fullmatch(rf"{re.escape(sno)}[A-Z]+", rows[i].get("sno") or ""):
        i += 1
    rows[i:i] = new


def set_rate(rows: list[dict], sno: str, old: str, new: str, sro: str) -> None:
    r = rows[_at(rows, sno)]
    if r["rate"] != old:
        raise ValueError(f"{sro}: S.No.{sno} reads {r['rate']!r}, the amendment says {old!r}")
    r["rate"], r["sro"] = new, sro


def apply_601(rows: list[dict]) -> None:
    """S.R.O. 601(I)/2015 (30 Jun 2015) on 568(I)/2014's Table, clause by clause as printed."""
    s = "601(I)/2015"
    insert_after(rows, "197", [_row("197A", "3402.2000", "Preparations put up for retail sale", "5", s),
                               _row("197B", "3402.9000", "Other", "5", s),
                               _row("197C", "4011.2090", "Other", "10", s),
                               _row("197D", "63.01", "Blankets and travelling rugs", "5", s)])
    insert_after(rows, "204", [_row("204A", "6907.1000", "Tiles, cubes and similar articles, whether or not rectangular, "
                                    "the largest surface area of which is capable of being enclosed in a square the side "
                                    "of which is less than 7 cm", "15", s),
                               _row("204B", "6907.9000", "Other", "15", s),
                               _row("204C", "6908.9090", "Other", "15", s)])
    insert_after(rows, "212", [_row("212A", "7204.4940", "Waste and scrap of Compressor", "5", s)])
    insert_after(rows, "220", [_row("220A", "72.16", "Angles, shapes and sections of iron or non-alloy steel", "15", s),
                               _row("220B", "72.17", "Wire of iron or non-alloy steel", "15", s)])
    omit(rows, ["221", "222", "223"])
    insert_after(rows, "224", [_row("224A", "72.25", "Flat rolled products of other alloy steel, of a width of 600 mm "
                                    "or more", "17.5", s),
                               _row("224B", "72.26", "Flat-rolled products of other alloy steel, of a width of less "
                                    "than 600 mm", "17.5", s)])
    substitute(rows, ["225"], [_row("225", "72.27", "Bars and rods, not rolled, in irregularly wound coils, of other "
                                    "alloy steel", "10", s)], s)
    omit(rows, ["226", "227"])
    set_rate(rows, "229", "15", "20", s)
    omit(rows, ["229B"])
    set_rate(rows, "229C", "15", "17.5", s)
    insert_after(rows, "229D", [_row("229E", "73.12", "Standard wire, ropes, cables, plaited bands, slings and the like, "
                                     "of iron or steel, not electrically insulated", "10", s),
                                _row("229F", "73.18", "Screws, bolts, nuts, coach screws hooks, rivets, cotters, "
                                     "cotter-pins, washers (including spring washers) and similar, article, of iron or "
                                     "steel.", "10", s)])
    insert_after(rows, "238", [_row("238A", "8212.1000", "Razors", "5", s),
                               _row("238B", "8212.2000", "Safety razor blades, including razor blade blanks in strips",
                                    "5", s),
                               _row("238C", "83.11", "Wire, rods, tubes, plates, electrodes and similar products, of "
                                    "base metal or of metal carbides, coated or cored with flux material, of a kind used "
                                    "for soldering, brazing, welding or deposition of metal or of metal carbides; wire "
                                    "and rods, of agglomerated base metal powder, used for metal spraying.", "10", s)])
    insert_after(rows, "309", [_row("309A", "9503.0010", "Wheeled toys designed to be ridden by children (for example, "
                                    "tricycles, scooters, pedal cars); dolls' carriages.", "5", s),
                               _row("309B", "9503.0090", "Other", "5", s)])


def entries(rows: list[dict], sro: str) -> list[dict]:
    out = []
    for r in rows:
        for c in r.get("codes") or []:
            rate = re.sub(r"\s*ad[\s.-]*val(?:orem)?\.?\s*$", "", str(r.get("rate") or "").strip(), flags=re.I)
            rate = re.sub(r"^(\d+(?:\.\d+)?)\s*%$", r"\1", rate)          # '15% ad valorem' -> '15'
            out.append({"code": c, "description": " ".join(str(r.get("description") or "").split()),
                        "rate": rate, "sro": r.get("sro") or sro})
    return out


def build(text: str, deepseek: str) -> dict:
    T = lambda f: os.path.join(text, f)  # noqa: E731
    years: dict[str, list[dict]] = {}
    checks: list[str] = []

    # ---- 568(I)/2014 -------------------------------------------------------------------------------
    jul14 = docx_rows(T("sros/20147161472621652UPDATE-SRO568(I)-2014-R.D.docx"))
    added_631 = {("3305.1000", "Shampoos"), ("9405.4090", "Other"), ("9617.0010", "Vacuum flasks"), ("9617.0020", "Other")}
    fy15_568 = [r for r in jul14 if (r["codes"][0], r["description"]) not in added_631]
    assert len(jul14) - len(fy15_568) == 4, "631(I)/2014's four rows not found in the Jul 2014 consolidation"
    checks.append(f"568 FY2015: {len(fy15_568)} rows (Jul 2014 consolidation {len(jul14)} minus 631(I)/2014's 4)")

    mar15 = numbered_rows(pdf_text(T("sros/2015327163133941UPDATE-SRO568I-2014-12.2.15.pdf")))
    nums = [int(r["sno"]) for r in mar15 if r["sno"].isdigit()]
    assert sorted(nums) == list(range(1, max(nums) + 1)), "Mar 2015 consolidation: S.Nos not 1..N"
    fy16_568 = [dict(r, sro=None) for r in mar15]
    r280 = fy16_568[_at(fy16_568, "280")]
    assert r280["codes"] == ["8517.1210"], r280
    omit(fy16_568, ["280"])                                        # 602(I)/2015, from 1 Jul 2015
    apply_601(fy16_568)                                            # 601(I)/2015, 30 Jun 2015
    checks.append(f"568 FY2016: {len(mar15)} rows (Mar 2015 consolidation) - S.No.280 (602) + 601 -> {len(fy16_568)}")

    mar16 = docx_rows(T("sros/20163251534131437UPDATE-SRO568(I)-2014-21.03.16-.docx"))
    t1177 = ds(deepseek, "s052")
    s236 = ds(deepseek, "s153")
    ours = {(r["codes"][0]): r["rate"] for r in mar16}
    theirs = {c: re.sub(r"[^\d.]", "", r["rate"]) for r in t1177["rows"] for c in r["codes"]}
    diff = {c: (theirs.get(c), ours.get(c)) for c in set(ours) | set(theirs) if theirs.get(c) != ours.get(c)}
    checks.append(f"568 FY2017: Mar 2016 consolidation {len(mar16)} rows; differs from the 1177(I)/2015 transcription "
                  f"at {len(diff)} codes (236(I)/2016's changes): {sorted(diff.items())}")
    # 236(I)/2016 "shall be effective till 30th day of June, 2016": on 1 July 2016 the Table is 1177(I)/2015's again.
    # Undo 236's changes on FBR's consolidation and require it to equal the transcription of 1177(I)/2015.
    s236_rows = {r["codes"][0] for r in s236["rows"]}
    fy17_568 = [dict(r, sro=None, rate=theirs.get(r["codes"][0], r["rate"])) for r in mar16
                if r["codes"][0] not in s236_rows]
    assert {r["codes"][0]: r["rate"] for r in fy17_568} == theirs, "Mar 2016 consolidation minus 236 != 1177"
    checks.append("568 FY2017: the Mar 2016 consolidation with 236(I)/2016 undone equals the 1177(I)/2015 transcription")

    t505 = ds(deepseek, "s292")
    t555 = ds(deepseek, "s316")
    fy18_568 = [dict(r, sro=None) for r in t505["rows"]]
    for bases in (["38"], ["39"], ["194"], ["299", "300", "301"], ["420"]):
        substitute(fy18_568, bases, snos_like(t555["rows"], *bases), "555(I)/2017")
    fy18_568[_at(fy18_568, "335")]["codes"] = ["7303.0000"]
    for after in ("374", "399"):
        new = [r for r in t555["rows"] if re.fullmatch(rf"{after}[A-Z]+", r.get("sno") or "")]
        i = _at(fy18_568, after) + 1
        fy18_568[i:i] = [dict(r, sro="555(I)/2017") for r in new]
    checks.append(f"568 FY2018: 505(I)/2017 {len(t505['rows'])} rows, 555(I)/2017 applied -> {len(fy18_568)}")

    # ---- 482(I)/2009 -------------------------------------------------------------------------------
    aug13 = numbered_rows(pdf_text(T("sros-base/201383014859860UPDATEDSRO482(I)2009august2013.pdf")))
    checks.append(f"482 Aug 2013 consolidation: {len(aug13)} rows, S.Nos {[r['sno'] for r in aug13]}")
    fy15_482 = [dict(r, sro=None) for r in aug13]
    fy17_482 = [dict(r, sro=None) for r in aug13]
    t1190 = ds(deepseek, "s055")   # 485(I)/2016 (from 1 Jul 2016) adds only a valuation rule for armoured cars
    substitute(fy17_482, ["36A", "37", "38", "38A", "38B", "39", "40", "41"], t1190["rows"], "1190(I)/2015")
    omit(fy17_482, ["2"])                                          # 1247(I)/2015
    t504 = ds(deepseek, "s291")    # + 556(I)/2017 (s317), its three clauses below
    fy18_482 = [dict(r, sro=None) for r in t504["rows"]]
    omit(fy18_482, ["9", "10"])                                    # 556(I)/2017 (a)
    for r in fy18_482:
        r["description"] = str(r.get("description") or "").replace(" (except electric hybrids)", "").replace(
            "(except electric hybrids)", "")                         # 556 (b)
        if r.get("sno") in ("47", "48"):
            r["codes"] = ["8703.9000" if c == "8703.9090" else c for c in r["codes"]]   # 556 (c)

    # ---- the single-item SROs -----------------------------------------------------------------------
    potato = [{"codes": ["0701.9000"], "description": "Ware potato", "rate": "25", "sro": "808(I)/2009"}]
    acrylic = [{"codes": ["3906.9030"], "description": "Pigment thickener", "rate": "5", "sro": "214(I)/2010"},
               {"codes": ["3906.9040"], "description": "Dispersing agent and acrylic thickeners", "rate": "5",
                "sro": "214(I)/2010"}]
    sugar = [{"codes": [c], "description": "Sugar", "rate": "20", "sro": "1043(I)/2014"}
             for c in ("1701.1200", "1701.1310", "1701.1390", "1701.1400", "1701.9910", "1701.9920")]
    w = "254(I)/2015"   # 30 Mar 2015, as printed (four rows)
    wheat = [{"sno": "1", "codes": ["1001.1900", "1001.9900"], "description": "Wheat", "rate": "25", "sro": w},
             _row("2", "1101.0010", "Wheat flour", "25", w), _row("3", "1102.9000", "Maida", "25", w),
             _row("4", "1102.9000", "Semolina", "25", w)]
    assert "1001.9900 Wheat 25 2. 1101.0010 Wheat flour 25" in pdf_text(T("sros/20153311435432222015sro254.pdf"))
    petrol = [dict(r, sro="392(I)/2015") for r in ds(deepseek, "s079")["rows"]]
    furnace = [dict(r, sro="393(I)/2015") for r in ds(deepseek, "s080")["rows"]]
    maize = [{"codes": ["1005.9000"], "description": "Maize", "rate": "30", "sro": "1248(I)/2015"}]
    wheat60 = [dict(r, rate="60", sro="854(I)/2016") for r in wheat if r.get("sno") == "1"]

    def year(parts):
        out = []
        for rows, sro in parts:
            out += entries(rows, sro)
        return out

    years["2015"] = year([(fy15_568, "568(I)/2014"), (fy15_482, "482(I)/2009"), (potato, "808(I)/2009"),
                          (acrylic, "214(I)/2010")])
    years["2016"] = year([(fy16_568, "568(I)/2014"), (fy15_482, "482(I)/2009"), (potato, "808(I)/2009"),
                          (acrylic, "214(I)/2010"), (sugar, "1043(I)/2014"), (wheat, "254(I)/2015"),
                          (petrol, "392(I)/2015"), (furnace, "393(I)/2015")])
    years["2017"] = year([(fy17_568, "568(I)/2014"), (fy17_482, "482(I)/2009"), (potato, "808(I)/2009"),
                          (acrylic, "214(I)/2010"), (sugar, "1043(I)/2014"), (furnace, "393(I)/2015"),
                          (maize, "1248(I)/2015")])
    years["2018"] = year([(fy18_568, "568(I)/2014"), (fy18_482, "482(I)/2009"), (potato, "808(I)/2009"),
                          (acrylic, "214(I)/2010"), (sugar, "1043(I)/2014"), (wheat60, "254(I)/2015"),
                          (furnace, "393(I)/2015"), (maize, "1248(I)/2015")])
    return {"source": ("Regulatory duty in force on 1 July 2014-2017, rebuilt from FBR's SROs and FBR's consolidated "
                       "copies (see scripts/claude_sros/build_rd_fy2015_2018.py)"),
            "checks": checks, "years": years}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    data = build(sys.argv[1], sys.argv[2])
    for c in data["checks"]:
        print(c[:600])
    for fy, es in data["years"].items():
        print(fy, len(es), "entries")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("written", OUT)
