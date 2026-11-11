"""fbr_additional_customs_duty - additional customs duty (ACD) by PCT code, from FBR's annual ACD notifications.

Source: ACD is levied under section 18(3) of the Customs Act by an SRO that replaces the previous one
each budget (FBR's customs SRO list). It isn't a table: it sets a rate per tariff slab and lists the
PCT codes charged differently, e.g. SRO 967(I)/2022:
    2% on goods in the 0%, 3% and 11% slabs, 4% on the 16% slab (5516.9300 and 5516.9400: 2%),
    6% on the 20% slab, 7% on 30% and higher slabs and specific rates (edible oils: 2%) …
    not levied on: goods in the 0% slab except PCT 72.04, seeds for sowing, …
`RULES` transcribes each budget SRO's slab rates and its code-level exceptions and exemptions; the
long code lists of the 2025 and 2026 SROs are read from the PDF (the scanner's OCR, each code checked
against that year's tariff). The SROs for FY2019-20 → FY2021-22 are bare scans: Claude read them from
the page images (2026-11) into `RULES`, codes as printed; SRO 845(I)/2021's list of ~300 exempt codes
is `_EXEMPT_845`. The code's slab is its customs duty that year (`customs_tariff_duties`
'cd'; a specific duty counts as "slabs of specific rates"). Exemptions by end use or importer (plant
and machinery for manufacturing, Fifth Schedule and Chapter 99 imports, baggage, …) cannot be decided
per code: the rate stored is the one a commercial import of the code pays.

FY2023-24 kept SRO 967(I)/2022: its amendments change nothing per code on 1 July 2023 — 1572(I)/2022
(oils, CKD cars, 35% on large cars) ran 22 Aug 2022 → 31 Mar 2023 (extended by 206(I)/2023) and
1954(I)/2022 only exempts stuck-up consignments of once-banned goods. Exemptions dated to end inside the
year count on 1 July (572(I)/2020's oilseeds and edible oils, to 30 Sep 2020). FY2018-19 and earlier are
not computed (FBR hosts SRO 630(I)/2018 only as an "UPDATED" copy). Rows: `customs_tariff_duties`
component 'acd' (source = the SRO). FBR answers GitHub's runners → `customs.yml`.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob
from ingestion.jobs.fbr_customs_tariff import fiscal_year, pdf_words
from ingestion.jobs.fbr_regulatory_duty import norm_code

S = "https://download1.fbr.gov.pk/SROs/"
OILS = ["15071000", "15079000", "15111000", "15119010", "15119020", "15119030", "15121100", "15121900",
        "15122100", "15122900", "15141100", "15141900", "15149100", "15149900"]
# seeds for sowing: the SROs print "1209.0000", which is heading 12.09 as a whole (no such 8-digit code)
SEEDS = ["09042120", "10061010", "1209"]
# 670(I)/2019 → 845(I)/2021: raw cotton and man-made staple fibre; dyes, solvents and other chemicals
FIBRES = ["5201", "5203", "55013000", "55031100", "55031900", "55033000", "55034000", "55039000", "55041000",
          "55049000", "55061000", "55063000", "55064000", "55069000", "55070000"]
CHEMS_2019 = ["29023000", "29141200", "29151290", "29339990", "32021000", "32029010", "32029090", "32041100",
              "32041300", "32071090", "32081090", "32089090", "34039910", "35069110", "35069190", "38123900",
              "39069020", "40051090", "40059900", "84532000", "96062920", "96062990"]
CHEMS_2020 = ["29023000", "29141200", "29151290", "29152100", "29339990", "32021000", "32029010", "32029090",
              "32041110", "32041120", "32041190", "32041300", "32071090", "32081090", "32089090", "34039910",
              "35069110", "35069190", "38123900", "39069020", "40051090", "40059900", "84532000", "96062920",
              "96062990"]
PHONES = ["85171211", "85171219"]
# 572(I)/2020 3(xviii): oil seeds and edible oils, exempt till 30 September 2020
OILSEEDS_2020 = ["12011000", "12051000", "12060000", "15071000", "15079000", "15111000", "15119020", "15119030",
                 "15121100", "15121900", "15141100", "15141900"]
# 845(I)/2021 3(xviii), as printed (five columns over two pages)
_EXEMPT_845 = """
2503.0000 2504.9000 2508.1000 2509.0000 2510.1000 2513.2020 2519.9090 2520.1020 2525.2000 2525.3000 2528.0000
2529.2200 2529.3000 2530.1000 2530.2000 2530.9010 2530.9020 2530.9030 2619.0000 2714.9000 2801.2000 2801.3000
2804.5000 2804.6100 2804.6900 2804.7000 2804.8000 2844.5000 2845.1000 2845.9000 2901.1010 2901.1090 2901.2200
2901.2910 2901.2990 2902.1100 2902.1920 2902.1990 2902.2000 2902.3000 2902.4200 2902.4400 2902.5000 2902.6000
2902.7000 2902.9010 2902.9090 2903.9200 2904.1010 2911.0000 2915.2100 2915.3920 2915.4000 2915.5000 3808.9140
3808.9180 3808.9210 3808.9290 3808.9910 3809.9110 3811.2100 3811.9000 3812.1000 3818.0000 3823.1200 3824.1000
3824.9100 3905.3000 3906.9040 3921.9010 4005.1020 4005.9100 4301.1000 4301.3000 4301.6000 4301.8000 4301.9000
4302.1100 4302.1990 4302.2000 4302.3000 5108.1000 5108.2000 5109.1000 5109.9000 5110.0000 5111.1100 5111.1900
5111.2000 5111.3000 5111.9000 5112.1100 5112.1900 5112.2000 5112.3000 5112.9000 5113.0000 5303.1090 5305.0020
5306.1000 5306.2000 5307.1000 5307.2000 5308.1000 5308.2000 5308.9000 5309.1100 5309.1900 5404.1900 5404.9000
5405.0000 5406.0000 5501.1000 5502.1000 5502.9010 5508.2000 5509.1100 5509.1200 5509.3100 5509.3200 5509.6100
5509.6200 5509.6900 5509.9100 5509.9200 5509.9900 5510.1100 5510.1200 5510.2000 5510.3000 5510.9000 5511.3000
6902.1090 8517.1211 8517.1219 2804.9000 2811.1100 2811.1920 2811.1990 2811.2910 2811.2990 2812.9000 2813.1000
2813.9000 2815.2000 2815.3000 2816.1010 2816.1090 2816.4000 2821.1020 2821.2000 2827.3200 2827.4100 2827.4900
2827.5100 2827.5900 2827.6000 2833.2500 2833.2700 2833.2940 2833.2990 2835.3900 2836.9930 2837.1100 2844.1000
2844.2000 2844.3000 2844.4000 2915.6010 2915.6020 2915.6030 2915.6090 2915.7090 2915.9000 2917.1110 2921.4510
2923.1000 2923.2000 2923.9010 2923.9090 2929.9020 2929.9030 2929.9090 2933.4100 2933.7990 2933.9100 2934.1090
2939.6900 2941.9020 2941.9030 2941.9070 3102.5010 3105.5100 3204.1300 3207.3000 3207.4090 3503.0020 3802.1000
3803.0000 3807.0000 3808.9130 4408.1000 4408.3100 4408.3900 4408.9090 5001.0000 5002.0000 5003.0000 5004.0000
5005.0000 5006.0000 5007.1000 5007.2000 5007.9000 5101.1100 5101.1900 5101.2100 5101.2900 5101.3000 5102.1100
5102.1900 5102.2000 5103.1000 5103.2000 5103.3000 5104.0000 5105.2900 5105.3100 5105.3900 5105.4000 5106.1000
5106.2000 5107.1000 5107.2000 5309.2100 5309.2900 5310.1000 5310.9010 5310.9090 5311.0000 5401.2010 5401.2090
5402.1100 5402.1900 5402.3100 5402.3200 5402.3400 5402.3900 5402.4490 5402.4800 5402.4900 5402.5100 5402.5300
5402.5900 5402.6100 5402.6300 5402.6900 5403.1000 5403.3200 5403.3300 5403.3910 5403.3990 5403.4100 5403.4200
5403.4900 5404.1100 5404.1200 7019.5900 3006.1090 5501.9000
""".replace(".", "").split()


@dataclass
class Rule:
    sro: str
    url: str
    slabs: dict                     # CD % -> ACD %; key 'higher' = this slab and above, 'specific' = specific duties
    higher_from: float | None       # the lowest slab that 'higher' covers
    codes: dict = field(default_factory=dict)        # 8-digit code -> ACD % (overrides the slab)
    exempt: list = field(default_factory=list)       # code prefixes not charged
    exempt_slabs: list = field(default_factory=list)  # CD slabs not charged, unless the code is in `unless`
    unless: list = field(default_factory=list)
    lists: list = field(default_factory=list)        # (start regex, end regex, ACD %): code lists read from the PDF


RULES: dict[int, Rule] = {
    2020: Rule("670(I)/2019", S + "20196291464532959SRO-670(I)-2019-28.6.2019.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, "higher": 7, "specific": 7}, 20,
               exempt=SEEDS + ["31"] + FIBRES + CHEMS_2019 + PHONES),
    2021: Rule("572(I)/2020", S + "20206301762147186SRO-572(I)2020.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, "higher": 7, "specific": 7}, 20,
               codes={c: 2 for c in OILS},
               exempt=SEEDS + ["31"] + FIBRES + CHEMS_2020 + PHONES + OILSEEDS_2020,
               exempt_slabs=[0], unless=["7204"]),
    2022: Rule("845(I)/2021", S + "202171873622728SRO-845(I)-2021,30.06.2021.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, 20: 6, "higher": 7, "specific": 7}, 30,
               codes={"55169300": 2, "55169400": 2, **{c: 2 for c in OILS}},
               # electric 2-3 wheelers (to 30 June 2025), cars up to 850cc (8703.211x, CKD and CBU)
               exempt=SEEDS + ["31"] + PHONES + ["87038030", "87116040", "87116060", "8703211"] + _EXEMPT_845,
               exempt_slabs=[0], unless=["7204"]),
    2023: Rule("967(I)/2022", S + "2022711173933595SRO967(I)2022.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, 20: 6, "higher": 7, "specific": 7}, 30,
               codes={"55169300": 2, "55169400": 2, **{c: 2 for c in OILS}},
               exempt=SEEDS + ["3901", "3902", "31"], exempt_slabs=[0], unless=["7204"]),
    # 967(I)/2022 still in force on 1 July 2023: its amendments change nothing per code (module docstring)
    2024: Rule("967(I)/2022", S + "2022711173933595SRO967(I)2022.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, 20: 6, "higher": 7, "specific": 7}, 30,
               codes={"55169300": 2, "55169400": 2, **{c: 2 for c in OILS}},
               exempt=SEEDS + ["3901", "3902", "31"], exempt_slabs=[0], unless=["7204"]),
    2025: Rule("929(I)/2024", S + "20246301563634161sro.929.pdf",
               {0: 2, 3: 2, 11: 2, 16: 4, 20: 6, "higher": 7, "specific": 7}, 30,
               codes={"55169300": 2, "55169400": 2, **{c: 2 for c in OILS}},
               exempt=SEEDS + ["27101210", "27101931", "27111100", "27111910", "3901", "3902", "5201", "5202",
                               "85414100", "85414200", "31"]),
    2026: Rule("1151(I)/2025", S + "202572975414701SRO1151OF2025.pdf",
               {15: 2, 20: 4, "higher": 6, "specific": 6}, 30,
               codes={c: 2 for c in OILS}, exempt=["87038030", "87116040", "87116060"],
               lists=[(r"following\s+pct\s+codes", r"\(ii\)|four\s+per\s*cen", 2)]),
    2027: Rule("1063(I)/2026", S + "20266301763307131063-2026.pdf",
               {20: 2, "higher": 4, "specific": 4}, 25,
               codes={c: 2 for c in OILS},
               # the scanner's OCR scrambles word order inside a line, so the markers are short
               lists=[(r"rate\s+of\s+four\s+per\s*cent", r"\(b\)\s+goods", 4),
                      (r"\(b\)\s+goods", r"\(c\)\s+goods", 2),
                      (r"six\s+per\s*cent", r"\(b\)\s+the\s+following", 6)]),
}


def list_codes(words: list[str], start: str, end: str, known: set[str]) -> list[str]:
    """8-digit codes printed between two clause markers (OCR'd), each checked against the tariff. Pure."""
    text = " ".join(words)
    low = text.lower()
    m = re.search(start, low)
    if not m:
        return []
    e = re.search(end, low[m.end():])
    seg = text[m.end(): m.end() + (e.start() if e else len(low))]
    toks = seg.replace(",", " , ").split()
    out: list[str] = []
    i = 0
    while i < len(toks):
        hit = None
        for k in (1, 2, 3):
            c = norm_code(toks[i:i + k])
            if c and len(c) == 9 and c.replace(".", "") in known:
                hit = (c.replace(".", ""), k)
                break
        if hit:
            out.append(hit[0])
            i += hit[1]
        else:
            i += 1
    return out


def acd_for(cd: tuple[float | None, str | None], code: str, rule: Rule, listed: dict[str, float]) -> float | None:
    """The ACD a commercial import of `code` pays under `rule`, given its CD (rate %, specific text). Pure."""
    if any(code.startswith(p) for p in rule.exempt):
        return None
    if code in listed:
        return listed[code]
    if code in rule.codes:
        return rule.codes[code]
    pct, txt = cd
    if pct is None:
        return rule.slabs.get("specific") if txt else None
    if pct in rule.exempt_slabs and not any(code.startswith(p) for p in rule.unless):
        return None
    if pct in rule.slabs:
        return rule.slabs[pct]
    if rule.higher_from is not None and pct >= rule.higher_from:
        return rule.slabs.get("higher")
    if pct in rule.exempt_slabs:          # e.g. 72.04 in the 0% slab: the slab's own rate
        return rule.slabs.get(pct)
    return None


@dataclass
class AcdRow:
    fy: int
    code: str
    rate_pct: float
    source: str


class FbrAdditionalCustomsDutyJob(IngestionJob):
    name = "fbr_additional_customs_duty"
    source = "FBR"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        years = sorted(RULES) if backfill else sorted(RULES)[-1:]
        return [FetchedFile(f"{fy}", self.http_get(RULES[fy].url), date.today()) for fy in years]

    def parse(self, f: FetchedFile):
        from app import db

        fy = int(f.filename)
        rule = RULES[fy]
        cds = {r["code"]: (None if r["rate_pct"] is None else float(r["rate_pct"]), r["rate_text"]) for r in db.query(
            "SELECT code, rate_pct, rate_text FROM customs_tariff_duties WHERE fy = %s AND component = 'cd'", (fy,))}
        if not cds:
            raise ValueError(f"FY{fiscal_year(fy)}: no tariff edition loaded")
        words = [w[3] for page in pdf_words(f.content) for w in sorted(page, key=lambda w: (round(w[1] / 4), w[0]))]
        listed: dict[str, float] = {}
        for start, end, rate in rule.lists:
            for c in list_codes(words, start, end, set(cds)):
                listed.setdefault(c, rate)
        rows = []
        for code, cd in cds.items():
            r = acd_for(cd, code, rule, listed)
            if r is not None:
                rows.append(AcdRow(fy, code, float(r), f"SRO {rule.sro}"))
        return rows

    def validate(self, records):
        for fy, n in Counter(r.fy for r in records).items():
            if n < 1000:
                raise ValueError(f"FY{fiscal_year(fy)}: only {n} codes carry ACD")
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
                       VALUES (%s, %s, 'acd', %s, NULL, %s)
                       ON CONFLICT (code, fy, component) DO UPDATE SET rate_pct = EXCLUDED.rate_pct,
                           source = EXCLUDED.source
                       WHERE (customs_tariff_duties.rate_pct, customs_tariff_duties.source)
                             IS DISTINCT FROM (EXCLUDED.rate_pct, EXCLUDED.source)""",
                    [(r.code, r.fy, r.rate_pct, r.source) for r in mine])
                n += cur.rowcount
                cur.execute("DELETE FROM customs_tariff_duties WHERE fy = %s AND component = 'acd' AND NOT (code = ANY(%s))",
                            (fy, [r.code for r in mine]))
        return n
