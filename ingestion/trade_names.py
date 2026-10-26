"""Readable names for PBS trade product codes (`trade_hs_codes`).

PBS prints a 30-character customs abbreviation per code ("LEATH BUFFAL HIDES,TANN,FINISH")
and, in the old fixed-width files, glues the unit on ("…FINISHSQM") or glues the tail of the
description into the unit column ("ACIDKG"). This module turns that into:

- `unit_clean`: the unit, split back out (KG, NO, MT, DOZ, SQM, …);
- `short_name`: PBS's text with the unit removed, abbreviations expanded, in sentence case;
- `hs_heading`: the official parent heading — HS 6-digit (HS 2022, else the newest edition
  that has the code) for 8-digit PCT codes, SITC Rev.3 5-digit for the 7-digit SITC codes
  (UN Statistics Division classification files, `data/reference/{H0..H6,S3}.json`);
- `display_name`: the official text when the code IS the official item (national digits
  "00"), otherwise `short_name`.

PBS's raw text stays in `description`. Pure functions; the reference files are loaded once.
"""
from __future__ import annotations

import functools
import json
import re
from pathlib import Path

REF_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
MAX_NAME = 100
HS_EDITIONS = ("H6", "H5", "H4", "H3", "H2", "H1", "H0")   # newest first

# ---- units ---------------------------------------------------------------------

UNITS = {
    "KG": "kg", "NO": "number", "MT": "tonnes", "DOZ": "dozen", "SQM": "sq m", "LTR": "litres",
    "GR": "grams", "GM": "grams", "CUBM": "cubic m", "CM": "cubic m", "VAL": "value only",
    "PAIR": "pairs", "PRS": "pairs", "CARAT": "carats", "MTR": "metres", "M": "metres",
    "TH NO": "thousands", "THNO": "thousands", "KWH": "kWh",
}
# longest first so "CUBM" wins over "M", "CARAT" over "T…"
_UNIT_RE = re.compile(r"^(.*?)(" + "|".join(sorted((u for u in UNITS if " " not in u),
                                                    key=len, reverse=True)) + r")$")


def split_unit(description: str, unit: str | None, classification: str = "") -> tuple[str, str]:
    """Return (description, unit) with glued units undone.

    - unit column carries a description tail: "ACIDKG" -> desc + " ACID", unit "KG"
      ("THNO" is a real unit: thousands of pieces);
    - old fixed-width SITC rows: 30 description characters with the unit glued on
      ("…DEMONSTRATONNO") and an empty unit column.
    """
    desc = (description or "").strip()
    u = (unit or "").strip().upper().replace(".", "")
    if u in UNITS:
        return desc, u
    if u:
        m = _UNIT_RE.match(u)
        if m and m.group(1):
            return f"{desc} {m.group(1)}".strip(), m.group(2)
        return desc, u
    if len(desc) > 30:
        head, tail = desc[:30], desc[30:].strip().upper()
        if tail in UNITS:
            return head.rstrip(), tail
    return desc, ""


# ---- abbreviations -------------------------------------------------------------
# Reviewed by hand against the official headings of the codes that use each token.
ABBREV = {
    # generic
    "oth": "other", "othr": "other", "otehr": "other", "o": "other", "ns": "n.e.s.", "nes": "n.e.s.",
    "n.s.": "n.e.s.", "n.e.s": "n.e.s.", "nesoi": "n.e.s.", "etc": "etc.", "excl": "excluding",
    "exc": "excluding", "incl": "including", "incor": "incorporating", "incorp": "incorporating",
    "w/o": "without", "wo": "without", "wit": "with", "w": "with", "cont": "containing",
    "conta": "containing", "contg": "containing", "upto": "up to", "exceed": "exceeding",
    "exc.": "excluding", "ex": "ex", "not": "not", "nt": "not", "un": "un", "simi": "similar",
    "simil": "similar", "sim": "similar", "throf": "thereof", "thereof": "thereof", "eqpt": "equipment",
    "eqp": "equipment", "equ": "equipment", "equip": "equipment", "equipments": "equipment",
    "acce": "accessories", "acc": "accessories", "acces": "accessories", "acesory": "accessories",
    "accessorie": "accessories", "pts": "parts", "pats": "parts", "par": "parts", "assem": "assembly",
    "asse": "assembly", "assemb": "assembly", "manuf": "manufacture", "manf": "manufacture",
    "mfg": "manufacturing", "manu": "manufacture", "prep": "prepared", "prepd": "prepared",
    "preprd": "prepared", "presv": "preserved", "prsv": "preserved", "preserv": "preserved",
    "pres": "preserved", "qlty": "quality", "wt": "weight", "weigh": "weight", "max": "maximum",
    "min": "minimum", "dia": "diameter", "dimen": "dimension", "temp": "temperature", "ind": "industrial",
    "indus": "industrial", "tech": "technical", "spec": "special", "spe": "special", "gen": "general",
    "reg": "regular", "std": "standard", "comb": "combed", "uncom": "uncombed", "ord": "ordinary",
    "ag": "agricultural", "agr": "agricultural", "agri": "agricultural", "hort": "horticultural",
    "lab": "laboratory", "med": "medical", "prop": "propelled", "selfprop": "self-propelled",
    "self-prop": "self-propelled", "sys": "system", "dev": "device", "devic": "device",
    "inst": "instruments", "instru": "instruments", "app": "apparatus", "appa": "apparatus",
    "appar": "apparatus", "aparatus": "apparatus", "appt": "apparatus", "appli": "appliances",
    "appl": "appliances", "mach": "machines", "mac": "machines", "machin": "machinery",
    "eng": "engines", "engin": "engines", "engins": "engines", "ele": "electric", "el": "electric",
    "elec": "electric", "elect": "electric", "electr": "electric", "mot": "motor", "motr": "motor",
    "veh": "vehicles", "vh": "vehicles", "vehi": "vehicles", "vehic": "vehicles", "vehicl": "vehicles",
    "tract": "tractors", "tracto": "tractors", "cyc": "cycles", "cyl": "cylinders", "cylind": "cylinders",
    "refrig": "refrigerating", "refri": "refrigerating", "hyd": "hydraulic", "pneum": "pneumatic",
    "cap": "capacity", "capa": "capacity", "contr": "control", "ctrl": "control", "reprod": "reproducing",
    "rec": "recording", "receiv": "receivers", "transm": "transmission", "transmision": "transmission",
    "phot": "photographic", "photo": "photographic", "optcal": "optical", "opt": "optical",
    "measur": "measuring", "proj": "projectors", "numercal": "numerical", "dielec": "dielectric",
    "discharg": "discharge", "ign": "ignition", "batery": "battery", "vacum": "vacuum",
    "vaccum": "vacuum", "kitch": "kitchen", "compres": "compressors", "compr": "compressors",
    "cuting": "cutting", "dril": "drilling", "weld": "welding", "grind": "grinding",
    # materials
    "fib": "fibres", "fibr": "fibres", "fibre": "fibres", "fila": "filament", "fil": "filament",
    "stap": "staple", "sta": "staple", "stapl": "staple", "syn": "synthetic", "synth": "synthetic",
    "synt": "synthetic", "art": "artificial", "artif": "artificial", "regen": "regenerated",
    "mmf": "man-made fibres", "manmad": "man-made", "manmade": "man-made", "cot": "cotton",
    "cott": "cotton", "coton": "cotton", "cotn": "cotton", "cottn": "cotton", "wol": "wool",
    "text": "textile", "textil": "textile", "texmat": "textile materials", "tx": "textile",
    "txt": "textile", "wov": "woven", "knt": "knitted", "kni": "knitted", "knit": "knitted",
    "knitd": "knitted", "croch": "crocheted", "cro": "crocheted", "croc": "crocheted",
    "yrn": "yarn", "thrd": "thread", "mul": "multiple", "cab": "cabled", "ble": "bleached",
    "bl": "bleached", "blea": "bleached", "bleac": "bleached", "blech": "bleached",
    "bleche": "bleached", "bleched": "bleached", "blec": "bleached", "unbl": "unbleached",
    "unble": "unbleached", "unb": "unbleached", "discont": "discontinuous", "discon": "discontinuous",
    "mx": "mixed", "mix": "mixed", "mixt": "mixtures", "mixwool": "mixed wool",
    "matl": "material", "materl": "material", "matrl": "material", "materi": "material",
    "stl": "steel", "stel": "steel", "st": "steel", "stainles": "stainless", "stain": "stainless",
    "alum": "aluminium", "al": "aluminium", "copp": "copper", "coper": "copper", "nickle": "nickel",
    "nick": "nickel", "irn": "iron", "i/s": "iron/steel", "aloy": "alloy", "ally": "alloy",
    "metl": "metal", "met": "metal", "mtl": "metal", "rub": "rubber", "rubb": "rubber",
    "ruber": "rubber", "vul": "vulcanised", "vulc": "vulcanised", "vulcaniz": "vulcanised",
    "plas": "plastic", "plast": "plastic", "pla": "plastic", "pl": "plastics", "glas": "glass",
    "gl": "glass", "porc": "porcelain", "cermic": "ceramic", "wd": "wood", "conif": "coniferous",
    "lea": "leather", "leath": "leather", "tann": "tanned", "uncoat": "uncoated", "coverd": "covered",
    "impreg": "impregnated", "unwrough": "unwrought", "pigiron": "pig iron",
    "hr": "hot-rolled", "h-rol": "hot-rolled", "rol": "rolled", "cr": "crocheted",
    "f-roll": "flat-rolled", "irr-wnd": "irregularly wound",
    # chemistry
    "deriv": "derivatives", "derivat": "derivatives", "derv": "derivatives", "der": "derivatives",
    "deri": "derivatives", "chl": "chloride", "phos": "phosphate", "nitrat": "nitrate",
    "sulphat": "sulphate", "sulph": "sulphur", "sulpha": "sulphate", "oxid": "oxide",
    "oxy": "oxy", "acet": "acetate", "acetat": "acetate", "acycl": "acyclic", "satur": "saturated",
    "unsat": "unsaturated", "carb": "carbonate", "inorg": "inorganic", "org": "organic",
    "organ": "organic", "compo": "compounds", "comp": "compounds", "subst": "substances",
    "polym": "polymers", "pol": "poly", "polys": "polyesters", "polysters": "polyesters",
    "fertz": "fertilisers", "pesti": "pesticides", "lubricat": "lubricating", "liq": "liquid",
    "liquified": "liquefied", "concent": "concentrated", "concen": "concentrated", "fract": "fractions",
    "frac": "fractions", "calc": "calcium", "mag": "magnesium", "arcyl": "acrylic", "acry": "acrylic",
    "acylic": "acrylic", "harmones": "hormones", "pencillin": "penicillin", "medicines": "medicines",
    "func": "functional", "funct": "functional", "elem": "elements", "unmix": "unmixed",
    # food & agriculture
    "froz": "frozen", "fz": "frozen", "frz": "frozen", "fr": "fresh", "fre": "fresh",
    "chill": "chilled", "chi": "chilled", "anim": "animals", "liv": "live", "livr": "liver",
    "ofal": "offal", "bov": "bovine", "bovin": "bovine", "buffal": "buffalo", "whol": "whole",
    "vege": "vegetables", "veg": "vegetables", "maiz": "maize", "virg": "virgin", "tobaco": "tobacco",
    "cru": "crude", "crd": "crude", "resid": "residues", "wast": "waste", "unstrip": "unstripped",
    "lentiles": "lentils", "akhrote": "walnuts", "guwar": "guar", "zeera": "cumin",
    # garments
    "gar": "garments", "garm": "garments", "wom": "women's", "grl": "girls'", "girl": "girls'",
    "bathrob": "bathrobes", "toruser": "trousers", "trou": "trousers", "pulover": "pullovers",
    "scarve": "scarves", "nedls": "needles", "knivs": "knives",
    # misc
    "tv": "TV", "cng": "CNG", "lpg": "LPG", "ckd": "CKD", "skd": "SKD", "cbu": "CBU", "akf": "AKF",
    "uav": "UAV", "frp": "FRP", "bopp": "BOPP", "pvc": "PVC", "hp": "hp", "cc": "cc", "kva": "kVA",
    "kw": "kW", "kv": "kV", "dx": "dtex", "nrs": "n.r.s.", "ucf": "UCF", "hcl": "HCl",
    "nk": "NK", "it": "IT", "rom": "ROM", "cd": "CD", "dvd": "DVD", "lcd": "LCD", "led": "LED",
    "ac": "AC", "dc": "DC", "usb": "USB", "gsm": "GSM", "ups": "UPS", "pc": "PC", "a/c": "aircraft",
    "fiting": "fittings", "fitt": "fittings", "fit": "fittings", "safty": "safety", "pums": "pumps",
    "trailor": "trailer", "braker": "breakers", "cabl": "cables", "wir": "wire", "ligh": "lighting",
    "displ": "display", "tub": "tubes", "fram": "frames", "embroid": "embroidered", "weav": "weaving",
    "cir": "circuits", "shet": "sheets", "plat": "plates", "sec": "sections", "sh": "sheets",
    "bord": "boards", "artcl": "articles", "artcle": "articles", "artic": "articles", "sew": "sewing",
    "move": "movements", "nit": "knitted", "vin": "vinyl", "dentst": "dentistry",
    "pains": "paints", "rbd": "RBD", "vanccines": "vaccines", "medici": "medicines", "ststic": "static",
    "handlom": "handloom", "kn": "knitted", "glov": "gloves", "mitten": "mittens",
    "pcas": "PVC", "battries": "batteries", "turb": "turbines", "thre": "thread", "fab": "fabrics", "drin": "drinking",
    "gla": "glasses", "polyestr": "polyesters", "filanent": "filament", "mileo": "mileo", "thi": "thick", "thik": "thick", "thic": "thick", "thn": "thin",
}
# Multi-character shorthands with punctuation, applied before word-level expansion.
PHRASES = [
    (r"\bi/na-s\b", "iron/non-alloy steel"), (r"\bir/na\b", "iron/non-alloy"), (r"\bi/na\b", "iron/non-alloy"),
    (r"\bi/s\b", "iron/steel"), (r"\bi\.s\.", "iron/steel"), (r"\ba/c\b", "aircraft"),
    (r"\bw/o\b", "without"), (r"\bn\.e\.s\.?", "n.e.s."), (r"\bn\.s\.?", "n.e.s."),
    (r"\bh\.r\.?", "hot-rolled"), (r"\bc\.r\.?", "cold-rolled"), (r"\bh-rol\b", "hot-rolled"),
    (r"\bc-rol\b", "cold-rolled"), (r"\bf-roll?\b", "flat-rolled"), (r"\bcold-roll\b", "cold-rolled"),
    (r"\birr-wnd\b", "irregularly wound"), (r"\bs-fib\b", "synthetic fibres"), (r"\bm/c\b", "machine"),
    (r"\bo/yrn\b", "other yarn"), (r"\bel-mot\b", "electric motor"), (r"\bel-\b", "electric "),
    (r"\bnon-ag\b", "non-agricultural"), (r"\bpr/met\w*", "precious metal"),
    (r"\bk/cr?\b", "knitted or crocheted"), (r"\bkn/cr\b", "knitted or crocheted"),
    (r"\bn[/-]knit\w*", "not knitted"), (r"\bnot/knit\w*", "not knitted"),
    (r"\bm/boys?\b", "men's/boys'"), (r"\bw/grl\b", "women's/girls'"), (r"\bw/girls?\b", "women's/girls'"),
    (r"\b(?:text?|txt|tex)\.? mat(?:e|l|erial)?\b", "textile materials"), (r"\bcomp-leather\b", "composition leather"),
    (r"\bl\.p\.g\.?", "LPG"), (r"\bsur/veten\w*", "surgical/veterinary"), (r"\bmill-?mad\b", "mill-made"),
    (r"\bo/text(?:ile)?\b", "other textile"), (r"\bo/spr\b", "other spirits"),
    (r"\bo/th\b", "other than"), (r"\bnotmore\b", "not more"), (r"\bmorethan\b", "more than"),
    (r"\bp/c\b", "plated or coated"), (r"\bs-q\b", "secondary quality"), (r"\bo/thn\b", "other than"),
    (r"\bw/out\b", "without"), (r"\bl/speaker", "loudspeaker"), (r"\bse-trailers?\b", "semi-trailers"),
    (r"\bh-?\.?\s?(\d{4}(?:\.\d{2,4})?)", r"heading \1"), (r"\bo-(?=[a-z])", "other "),
    (r"\bot\b", "other"), (r"\bcart?ridg\b", "cartridges"), (r"\bbatt?ries\b", "batteries"),
    (r"\bsqm\b", "sq m"),
]
_PHRASES = [(re.compile(p, re.I), r) for p, r in PHRASES]

# Some tokens mean different things by chapter (HS 2-digit / SITC 2-digit prefix).
CHAPTER_ABBREV = {
    # iron & steel: FR = flat-rolled, SEC = secondary (quality), TH = thickness
    **{ch: {"fr": "flat-rolled", "sec": "secondary", "th": "thickness", "qlty": "quality", "cr": "cold-rolled",
            "na": "non-alloy", "cro": "cross", "s-q": "secondary quality"}
       for ch in ("72", "73", "67", "69")},
    # fish & food: CHL = chilled
    **{ch: {"chl": "chilled", "lvr": "livers", "fre": "fresh"} for ch in ("03", "02", "04", "07", "08")},
}

# all-caps acronyms kept as they are
_KEEP_UPPER = {v for v in ABBREV.values() if v.isupper()} | {"PCT", "HS", "SITC", "PVC", "LED", "LCD"}

_TOKEN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def expand(text: str, chapter: str = "") -> str:
    """Expand PBS abbreviations and tidy spacing/punctuation (lower case except acronyms)."""
    local = CHAPTER_ABBREV.get(chapter, {})
    t = text.strip()
    for rx, rep in _PHRASES:
        t = rx.sub(rep, t)
    t = re.sub(r"\s*,\s*", ", ", t)
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"(?<=[A-Za-z])\(", " (", t)                      # "BULLS(OXEN)O/TH"
    t = re.sub(r"\)(?=[A-Za-z])", ") ", t)
    t = re.sub(r"(?<=[A-Za-z]{3})\.(?=[A-Za-z]{3,})", " ", t)    # "ALUM POWDERS.NON-LAMELLAR"

    def tok(m: re.Match) -> str:
        w = m.group(0)
        if w in ("iron", "steel", "aircraft", "without", "rolled", "non", "alloy", "wound",
                 "irregularly", "flat", "hot", "cold", "machine", "electric", "motor",
                 "precious", "metal", "secondary", "quality", "synthetic", "fibres", "other", "yarn",
                 "agricultural", "sq", "m"):
            return w                                   # produced by PHRASES above
        key = w.lower()
        return local.get(key) or ABBREV.get(key) or key

    out = _TOKEN.sub(tok, t)
    out = re.sub(r"\s+", " ", out).strip(" ,;")
    return out


def sentence_case(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def short_name(description: str, unit: str | None = None, classification: str = "",
               code: str = "") -> str:
    desc, _ = split_unit(description, unit, classification)
    return sentence_case(expand(desc, code[:2]))


# ---- official headings -------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _ref(code: str) -> dict[str, dict]:
    path = REF_DIR / f"{code}.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))["results"]
    return {r["id"]: r for r in rows}


def _text(r: dict) -> str:
    return r["text"].split(" - ", 1)[-1].strip()


def _full_text(ref: dict[str, dict], code: str) -> str | None:
    """An item's text; relative texts ("- Other", "-- colour", "....mixed with wool") are
    prefixed with their parent's text."""
    r = ref.get(code)
    if r is None:
        return None
    t = _text(r)
    parts = [t]
    while re.match(r"^[-.]+\s*", parts[0]) and r.get("parent") in ref and r["parent"] != "TOTAL":
        parts[0] = re.sub(r"^[-.]+\s*", "", parts[0])
        r = ref[r["parent"]]
        parts.insert(0, _text(r))
    parts[0] = re.sub(r"^[-.]+\s*", "", parts[0])
    return "; ".join(p for p in parts if p)


def reference_available() -> bool:
    """True when the UN classification files are present (a load without them would silently
    fall back to PBS wording for every code)."""
    return bool(_ref("H6")) and bool(_ref("S3"))


def official_heading(code: str, classification: str) -> tuple[str | None, str | None]:
    """(heading text, edition) for the code's official parent item."""
    if classification == "HS" and len(code) >= 6:
        six = code[:6]
        for ed in HS_EDITIONS:
            t = _full_text(_ref(ed), six)
            if t:
                return t, ed
    if classification == "SITC" and len(code) >= 5:
        t = _full_text(_ref("S3"), code[:5])
        if t:
            return t, "S3"
    return None, None


def clean(code: str, classification: str, description: str, unit: str | None) -> dict:
    """All derived name fields for one `trade_hs_codes` row."""
    if classification == "total":
        return {"display_name": "All products", "short_name": "All products",
                "hs_heading": None, "unit_clean": None}
    desc, u = split_unit(description, unit, classification)
    short = sentence_case(expand(desc, code[:2])) or description
    heading, _ = official_heading(code, classification)
    national = code[6:] if classification == "HS" else code[5:]
    # the official text is the name when the code IS the official item, unless it is a
    # paragraph (some HS texts run to 300 characters); the full text stays in hs_heading
    exact = (heading and national.strip("0") == "" and len(heading) <= MAX_NAME
             and not re.match(r"(?i)^other\b", heading))
    return {"display_name": heading if exact else short, "short_name": short,
            "hs_heading": heading, "unit_clean": u or None}


# ---- page names and URL slugs (pakdatahub.com/trade/products/{code}-{slug}) -----------------

def heading_text(code: str) -> str | None:
    """Official text of an HS chapter (2 digits) or heading (4 digits), from the newest edition
    that has it."""
    for ed in HS_EDITIONS:
        r = _ref(ed).get(code)
        if r:
            return _text(r)
    return None


def _cut(s: str, n: int) -> str:
    """Cut at the last comma that fits, else the last space."""
    if len(s) <= n:
        return s
    at = s.rfind(", ", 0, n)
    if at <= 0:
        at = s.rfind(" ", 0, n)
    return (s[:at] if at > 0 else s[:n]).strip(" ,;")


def short_title(text: str, max_len: int = 70) -> str:
    """An official heading cut to a title: parentheticals dropped, the qualifier after ";" kept
    when it fits ("Cotton; not carded or combed" -> "Cotton, not carded or combed"), long lists
    cut at a comma. The full text stays on the page."""
    t = text or ""
    while True:
        u = re.sub(r"\s*\([^()]*\)", "", t)
        if u == t:
            break
        t = u
    parts = [p.strip(" ,") for p in t.split(";") if p.strip(" ,")]
    if not parts:
        return (text or "").strip()
    head = parts[0]
    if len(parts) > 1:
        both = f"{head}, {parts[1]}"
        if len(both) <= max_len:
            return both
        if len(parts[1]) <= 30:
            room = _cut(head, max_len - 2 - len(parts[1]))
            if len(room) >= 20:
                return f"{room}, {parts[1]}"
    return _cut(head, max_len)


def slugify(text: str, max_len: int = 60) -> str:
    """URL words: lower case, "&" -> "and", apostrophes dropped, any other run of characters a
    hyphen, cut at a word boundary within max_len. Mirrored by `slugify` in
    frontend/src/lib/pakdata/trade.ts — change both together."""
    s = re.sub(r"['\u2019]", "", (text or "").lower().replace("&", " and "))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) > max_len:
        at = s.rfind("-", 0, max_len + 1)
        s = s[:at] if at > 0 else s[:max_len]
    return s
