"""ismo_merit_order - the Economic Merit Order: every thermal power plant's cost per unit, Jun-2024 →.

Source: ISMO's "Economic Merit Order" office orders (ismo.gov.pk documents, category merit-order),
issued whenever fuel prices change (fortnightly to weekly). Each lists the thermal plants (GENCOs
and IPPs) in dispatch order with:

    Sr.No | Plant | Fuel type | Other cost | Fuel cost | VO&M cost | Specific cost (Rs/kWh) | last rank

The orders are photographed tables, so `scripts/ocr_merit_orders.py` (workstation, OCR engine)
turns every page into positioned tokens and this job reads the tokens: cost columns are found by
clustering the 4-decimal figures, rows by their height, and a row is kept only if
other + fuel + VO&M = specific cost (to rounding) — a misread digit fails that check and the row
is dropped rather than stored. Plant names are matched across orders on a letters-only key and
shown in their most fully spaced spelling.

Series (dims `plant`, `fuel`; dated at the order's effective date):
    energy.power.merit_order.specific_cost   Rs/kWh, the cost the dispatcher ranks plants on
    energy.power.merit_order.fuel_cost       Rs/kWh, its fuel component
    energy.power.merit_order.rank            position in the merit order (1 = dispatched first)
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record
from ingestion.plants_pk import canon_plant

TOKENS_PATH = os.getenv("ISMO_MERIT_TOKENS", "/app/ismo_merit_tokens.json")
SOURCE_URL = "https://www.ismo.gov.pk/library"
_COST = re.compile(r"^\d{1,3}\.\d{3,5}$")
_INT = re.compile(r"^\d{1,3}$")
_FUELS = {"gas": "Gas", "coal": "Coal", "rlng": "RLNG", "d-gas": "Domestic gas", "dgas": "Domestic gas",
          "rfo": "Furnace oil", "hsd": "Diesel", "lsfo": "Low-sulphur furnace oil", "gaspg": "Gas (PG)",
          "gas pg": "Gas (PG)", "gasbdr(pel)": "Gas (Bdr, PEL)", "gas bdr(pel)": "Gas (Bdr, PEL)",
          "nuclear": "Nuclear", "mixed": "Mixed"}

_C = dict(module="energy", frequency="irregular", source="ISMO", source_url=SOURCE_URL)
_TAIL = (" for every thermal power plant in ISMO's Economic Merit Order (dimensions plant and fuel), "
         "at each revision since June 2024.")
CATALOG = [
    dict(id="energy.power.merit_order.specific_cost", name="Power plant cost per unit (merit order)",
         unit="pkr_per_kwh", min_value=0, max_value=300,
         description="The cost per unit of electricity on which Pakistan's grid dispatches each thermal power "
                     "plant (fuel + variable O&M + other), Rs per kWh," + _TAIL, **_C),
    dict(id="energy.power.merit_order.fuel_cost", name="Power plant fuel cost per unit (merit order)",
         unit="pkr_per_kwh", min_value=0, max_value=300,
         description="The fuel cost per unit of electricity of each thermal power plant, Rs per kWh," + _TAIL, **_C),
    dict(id="energy.power.merit_order.rank", name="Power plant position in the merit order",
         unit="rank", min_value=1, max_value=200,
         description="Each thermal power plant's place in the dispatch order (1 = cheapest, dispatched first)"
                     + _TAIL, **_C),
]


def _center(b):
    xs, ys = [p[0] for p in b], [p[1] for p in b]
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, min(xs), max(xs)


def _num(t: str) -> float | None:
    t = t.replace(",", "").replace(" ", "").rstrip("'`.")
    try:
        return float(t)
    except ValueError:
        return None


def _rows(tokens: list[dict]) -> list[list[tuple]]:
    items = sorted(((*_center(t["box"]), t["text"].strip()) for t in tokens if t["text"].strip()), key=lambda r: r[1])
    heights = sorted(abs(t["box"][2][1] - t["box"][0][1]) for t in tokens)
    tol = (heights[len(heights) // 2] if heights else 20) * 0.6
    rows: list[list[tuple]] = []
    for it in items:
        if rows and abs(it[1] - sum(r[1] for r in rows[-1]) / len(rows[-1])) <= tol:
            rows[-1].append(it)
        else:
            rows.append([it])
    return [sorted(r, key=lambda r: r[0]) for r in rows]


def _clusters(xs: list[float], gap: float, min_n: int = 2) -> list[float]:
    out: list[list[float]] = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= gap:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(c) / len(c) for c in out if len(c) >= min_n]


def parse_page(tokens: list[dict], width: float) -> list[dict]:
    """One OCR'd page → [{rank, plant, fuel, other, fuel_cost, vom, specific}], checksum-verified. Pure."""
    rows = _rows(tokens)
    cost_x = [cx for r in rows for cx, _, _, _, t in r if _COST.match(t.replace(",", ""))]
    cols = _clusters(cost_x, width * 0.03)
    if len(cols) < 3:
        return []
    # the specific-cost column is the rightmost; VO&M left of it; fuel left of that; 'other' (if any) first
    spec_x, vom_x, fuel_x = cols[-1], cols[-2], cols[-3]
    other_x = cols[-4] if len(cols) >= 4 else None
    left_edge = (other_x or fuel_x) - width * 0.05
    # the Sr.No column: small integers at the far left, on most rows
    sr = _clusters([cx for r in rows for cx, _, _, _, t in r if _INT.match(t) and cx < width * 0.25], width * 0.015, 5)
    sr_x = sr[0] if sr else None
    out = []
    for r in rows:
        nums = {"specific": None, "vom": None, "fuel": None, "other": 0.0}
        for cx, _, x0, x1, t in r:
            v = _num(t)
            if v is None or not _COST.match(t.replace(",", "")) and not (t.replace(",", "").count(".") == 1):
                continue
            targets = [("specific", spec_x), ("vom", vom_x), ("fuel", fuel_x)] + ([("other", other_x)] if other_x else [])
            name, tx = min(targets, key=lambda k: abs(k[1] - cx))
            if abs(tx - cx) < width * 0.04:
                nums[name] = v
            elif other_x is None and fuel_x - width * 0.12 < cx < fuel_x:
                nums["other"] = v  # 'other cost' printed on one row only: too few to form a column
        if None in (nums["specific"], nums["vom"], nums["fuel"]):
            continue
        if abs(nums["other"] + nums["fuel"] + nums["vom"] - nums["specific"]) > 0.0015:
            continue  # a misread digit: leave the row out
        left = [(cx, t) for cx, _, x0, x1, t in r if x1 < left_edge and not _COST.match(t.replace(",", ""))]
        in_sr = (lambda cx: sr_x is not None and abs(cx - sr_x) < width * 0.02)
        is_rank = (lambda cx, t: _INT.match(t) and int(t) <= 150 and in_sr(cx))  # '747' MW Guddu is a name
        rank = next((int(t) for cx, t in left if is_rank(cx, t)), None)
        words = [t for cx, t in left if not is_rank(cx, t)]
        fuel = None
        for i in range(len(words) - 1, -1, -1):
            key = re.sub(r"\s+", " ", words[i].lower())
            if key in _FUELS or key.replace(" ", "") in _FUELS:
                fuel = _FUELS.get(key) or _FUELS[key.replace(" ", "")]
                words = words[:i] + words[i + 1:]
                break
            m = re.search(r"\b(d-gas|rlng|coal|gas|rfo|hsd|lsfo)$", key)
            if m and i == len(words) - 1:
                fuel = _FUELS[m.group(1)]
                words[i] = words[i][: m.start()].strip()
                break
        plant = re.sub(r"\s+", " ", " ".join(w for w in words if w)).strip(" |-")
        m = re.match(r"^(\d{1,3})\.?\s+(.+)$", plant)  # the serial number read into the name
        if m and int(m.group(1)) <= 150 and not m.group(2).upper().startswith("MW"):
            rank, plant = rank or int(m.group(1)), m.group(2)
        if not plant or fuel is None or re.search(r"[$@#%&*]", plant):  # OCR junk in the name
            continue
        out.append(dict(rank=rank, plant=plant, fuel=fuel, specific=nums["specific"], fuel_cost=nums["fuel"]))
    return out


def effective_date(title: str, url: str = "") -> date | None:
    """The order's effective date: from the file name when it carries one (ISMO's title for the
    16-04-2025 order says 2024), else from the title's '(dd-mm-yyyy)'. Pure."""
    for text, pat in ((url.rsplit("/", 1)[-1], r"(\d{1,2})-(\d{1,2})-(\d{2,4})"),
                      (title, r"\((\d{1,2})-(\d{1,2})-(\d{4})\)")):
        m = re.search(pat, text)
        if m:
            y = int(m.group(3))
            try:
                return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
    return None


def _key(name: str) -> str:
    k = re.sub(r"[^a-z0-9]", "", name.lower())
    return k.replace("l", "i").replace("1", "i").replace("0", "o")


def tidy_name(name: str) -> str:
    """OCR reads Roman numerals as l/1 ('Block-l', 'B-Il', 'B-IlI'); unit numbers stay. Pure."""
    return re.sub(r"(?<!Unit )-([Il1]{1,3})\b", lambda m: "-" + m.group(1).replace("l", "I").replace("1", "I"), name)


def signature(name: str) -> tuple:
    """The parts of a plant name that tell units apart and must match exactly before two spellings
    are merged: (CC)/(OC)/(HSD), unit numbers, upto/above a threshold. Pure."""
    n = name.lower().replace("(0c)", "(oc)").replace("(o0)", "(oc)").replace("（", "(")
    tags = tuple(sorted(set(re.findall(r"\((cc|oc|hsd)\)", n))))
    units = tuple(re.sub(r"\s", "", u) for u in re.findall(r"unit\s*-?\s*(\d+(?:\s*-\s*\d+)?)", n))
    limit = tuple(re.findall(r"\b(upto|above)\b", n))
    return tags, units, limit


def to_records(bundle: dict) -> list[Record]:
    """Every document → records. Plant spellings are clustered (OCR reads 'Halmore' as 'Haimore'):
    keys within 0.85 similarity share the cluster's most frequent spelling; a name read only once
    across all the orders is an OCR artefact and is dropped. Pure."""
    import difflib

    rows: list[tuple[date, dict]] = []
    for doc in bundle.get("documents", []):
        d = effective_date(doc.get("title") or "", doc.get("url") or "")
        if d is None:
            continue
        for page in doc.get("pages", []):
            rows += [(d, r) for r in parse_page(page["tokens"], page["width"])]
    freq = Counter(_key(r["plant"]) for _, r in rows)
    sig = {_key(r["plant"]): signature(r["plant"]) for _, r in rows}
    canon: dict[str, str] = {}
    heads: list[str] = []
    for k, _ in freq.most_common():
        hit = next((h for h in heads if sig[h] == sig[k] and difflib.SequenceMatcher(None, k, h).ratio() >= 0.85), None)
        if hit is None:
            heads.append(k)
            hit = k
        canon[k] = hit
    spellings: dict[str, Counter] = defaultdict(Counter)
    for _, r in rows:
        spellings[canon[_key(r["plant"])]][r["plant"]] += 1
    # display: among the frequent spellings (>= 10% of reads), the most fully spaced one — OCR often
    # drops spaces, so the commonest read can be 'LuckyElectricPowerCompany'
    best = {}
    for h, c in spellings.items():
        n = sum(c.values())
        if n < 3:
            continue  # a fragment ('B-I (Unit 11-13)') or a one-off misread
        pool = [s for s in c if c[s] >= max(2, 0.1 * n)] or list(c)
        best[h] = tidy_name(max(pool, key=lambda s: (s.count(" "), c[s])))
    out: list[Record] = []
    seen: set = set()
    for d, r in rows:
        head = canon[_key(r["plant"])]
        if head not in best:
            continue
        dims = {**canon_plant(best[head]), "fuel": r["fuel"]}   # registry name + unit/config/tranche
        k = (d, tuple(sorted(dims.items())))
        if k in seen:  # the same plant read twice on one order: keep the first
            continue
        seen.add(k)
        out.append(Record("energy.power.merit_order.specific_cost", d, r["specific"], dims))
        out.append(Record("energy.power.merit_order.fuel_cost", d, r["fuel_cost"], dims))
        if r["rank"]:
            out.append(Record("energy.power.merit_order.rank", d, float(r["rank"]), dims))
    return out


class IsmoMeritOrderJob(IngestionJob):
    name = "ismo_merit_order"
    source = "ISMO"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        if not os.path.exists(TOKENS_PATH):
            return []
        return [FetchedFile("ismo_merit_tokens.json", open(TOKENS_PATH, "rb").read(), date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        return to_records(json.loads(f.content))
