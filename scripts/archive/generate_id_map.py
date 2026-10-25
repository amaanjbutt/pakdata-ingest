"""Generate a clean-id migration map for every active series.

Read-only: computes old_id -> new_id under the `topic.group.item` scheme and
writes /tmp/id_map.json. Mutates nothing. Review the sample before applying.

Leaf slugs are derived from the OFFICIAL series name by stripping the word-level
common prefix/suffix shared across a dataset (what remains is the distinguishing
part — the country, sector, fuel type). Topics are curated for high-value
datasets and fall back to a subject-based default.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from app import db

# ---- topic assignment -------------------------------------------------------
# Keyed on the dataset token (4th segment of TS_GP_<SUBJ>_<DATASET>_<FREQ>).
DATASET_TOPIC = {
    "PSAUTO": "auto", "ELECGEN": "power", "POLSALE": "fuel",
    "SALEFERT": "agriculture.fertilizer", "SALEFMCG": "industry.fmcg",
    "QGDP1516": "gdp", "PAKGDP15": "gdp", "PAKGDP80": "gdp", "FISPAKGDP": "gdp",
    "WR": "remittances", "PAKRES": "reserves",
    "FAERPKR": "fx.rate.avg", "FMEERPKR": "fx.rate.monthend",
    "FAERUSD": "fx.usd.avg", "FMEERUSD": "fx.usd.monthend", "REERNEER": "fx.effective",
    "KSORDA": "roshan_digital", "EPUI": "sentiment.uncertainty",
    "PAKPOP": "population", "PAKPOPAS": "population", "POPECOCAT": "population",
    "LITBYAGE": "education.literacy", "LITBYEDU": "education.literacy",
    "EDUENROL": "education.enrolment", "EDUINST": "education.institutions",
    "TCHRINPAK": "education.teachers", "NMHE": "health",
    "BBTD": "payments.branchless", "PSQD": "payments",
    "SGADVNPL": "banking.npl", "ADNPLMFB": "banking.npl.mfb",
}
# fall back on the subject segment (3rd token).
SUBJECT_TOPIC = {
    "BOP": "bop", "ES": "external", "FI": "investment",
    "ED": "debt.external", "PKDP": "debt", "PDL": "debt",
    "ER": "fx", "BAM": "banking", "MFS": "money", "BS": "banking",
    "FSA": "corporate", "RLS": "industry", "RS": "gdp", "GA": "gdp",
    "RL": "social", "PT": "inflation", "PF": "fiscal", "IR": "rates", "EXT": "external",
}
# Trade-by-X datasets deserve richer topics (name common-prefix does the leaf).
TRADE_HINTS = {
    "XRECCOU": "trade.exports.by_country", "MPAYCOU": "trade.imports.by_country",
    "XRECCOMM": "trade.exports.by_commodity", "MPAYCOMM": "trade.imports.by_commodity",
    "XRECHS2": "trade.exports.hs2", "MPAYHS2": "trade.imports.hs2",
    "EXPCTY": "trade.exports.by_country", "IMPCTY": "trade.imports.by_country",
}

# Explicit new ids for curated non-EasyData flagships (no dataset code).
FLAGSHIP_PREFIX = [
    (r"^kibor\.", "rates.kibor."),
    (r"^pkrv\.", "rates.pkrv."),
    (r"^pkisrv\.", "rates.pkisrv."),
    (r"^policy_rate", "rates.policy"),
    (r"^auction\.", "rates.auction."),
    (r"^cpi\.", "inflation.cpi."),
    (r"^wpi", "inflation.wpi"),
    (r"^lsm\.", "industry.lsm."),
    (r"^commodity\.", "commodities."),
    (r"^telecom\.", "telecom."),
    (r"^payments\.", "payments."),
    (r"^sme\.", "sme."),
    (r"^fx\.avg\.", "fx.rate.avg."),
    (r"^fx\.monthend\.", "fx.rate.monthend."),
    (r"^fx\.reer", "fx.effective.reer"),
    (r"^fx\.neer", "fx.effective.neer"),
    (r"^fx\.interbank\.", "fx.interbank."),
    (r"^remittances\.", "remittances."),
]

_STOP = {"of", "the", "in", "and", "by", "to", "for", "a", "an", "at", "on",
         "per", "from", "s", "pak", "pakistan", "all"}


def slugify(text: str, keep_stop: bool = False) -> str:
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    words = [w for w in text.split() if keep_stop or w not in _STOP]
    return "_".join(words)[:48].strip("_")


def _tokens(name: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).split()


def _common_affix(names: list[str]):
    toks = [_tokens(n) for n in names if n]
    if len(toks) < 2:
        return 0, 0
    pre = 0
    while all(len(t) > pre and t[pre] == toks[0][pre] for t in toks):
        pre += 1
    suf = 0
    while all(len(t) > pre + suf and t[-1 - suf] == toks[0][-1 - suf] for t in toks):
        suf += 1
    return pre, suf


def topic_for(dataset_code: str | None, module: str) -> str:
    if not dataset_code:
        return module
    parts = dataset_code.split("_")
    subj = parts[2] if len(parts) > 2 else ""
    dset = parts[3] if len(parts) > 3 else ""
    return (TRADE_HINTS.get(dset) or DATASET_TOPIC.get(dset)
            or SUBJECT_TOPIC.get(subj) or module)


def flagship_new_id(old: str) -> str | None:
    for pat, repl in FLAGSHIP_PREFIX:
        if re.match(pat, old):
            return re.sub(pat, repl, old)
    return None


def main():
    rows = db.query(
        "SELECT id, name, module, easydata_dataset_code AS dc FROM series WHERE is_active"
    )
    mapping: dict[str, str] = {}
    used: dict[str, int] = defaultdict(int)

    def claim(base: str) -> str:
        used[base] += 1
        return base if used[base] == 1 else f"{base}_{used[base]}"

    # Pass 1: flagship curated ids (clean already) take precedence over any
    # dataset-derived name, regardless of whether they carry an EasyData code.
    dataset_rows = []
    for r in rows:
        fid = flagship_new_id(r["id"])
        if fid:
            mapping[r["id"]] = claim(fid)
        else:
            dataset_rows.append(r)

    # Pass 2: everything else, grouped by dataset for common-affix leaf extraction.
    by_ds = defaultdict(list)
    for r in dataset_rows:
        by_ds[r["dc"] or f"__mod__{r['module']}"].append(r)

    for dc, group in by_ds.items():
        real_dc = None if str(dc).startswith("__mod__") else dc
        topic = topic_for(real_dc, group[0]["module"])
        last_topic_word = topic.split(".")[-1]
        pre, suf = _common_affix([r["name"] for r in group])
        for r in group:
            toks = _tokens(r["name"])
            core = toks[pre: len(toks) - suf] if (pre or suf) else toks
            core = [w for w in core if w not in _STOP]
            # drop a leading leaf word that just repeats the topic
            while core and core[0] == last_topic_word:
                core.pop(0)
            leaf = "_".join(core)[:44].strip("_") or (slugify(r["name"]) or "value")
            mapping[r["id"]] = claim(f"{topic}.{leaf}")

    json.dump(mapping, open("/tmp/id_map.json", "w"), indent=0)

    # ---- report ----
    print(f"series mapped: {len(mapping)}")
    print(f"distinct new ids: {len(set(mapping.values()))} (collisions auto-suffixed)")
    topics = defaultdict(int)
    for v in mapping.values():
        topics[v.split(".")[0]] += 1
    print("top topics:", dict(sorted(topics.items(), key=lambda x: -x[1])[:20]))
    print("\n--- SAMPLE (old -> new) ---")
    picks = ["fx.avg.usd", "kibor.3m", "cpi.national.yoy", "payments.raast.p2p.value",
             "gp_rls_psauto_m.as_001001", "gp_rls_elecgen_m.e_002000",
             "gp_rs_qgdp1516_q.qgdp00080000", "gp_rl_pakpop_y.pakpop00800",
             "gp_bop_xreccou_m.expc00860", "gp_bam_advbybor_m.abs0000100"]
    for old in picks:
        if old in mapping:
            print(f"  {old:34} -> {mapping[old]}")
    print("\n--- random long-tail sample ---")
    import random
    random.seed(1)
    for old in random.sample(list(mapping), 18):
        r = next(x for x in rows if x["id"] == old)
        print(f"  {mapping[old]:42} <= {r['name'][:52]}")


if __name__ == "__main__":
    main()
