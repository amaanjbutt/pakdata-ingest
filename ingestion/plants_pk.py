"""One registry of Pakistan's power plants, shared by every energy source.

ISMO's daily output sheets, ISMO's merit orders and NEPRA's annual State of Industry tables
each spell plants their own way ("LALPIR (Pvt) Ltd", "Lalpir Power", "AES Lalpir power
limited"), and the merit order folds operating details into the name ("UCH (upto 152,375
MWh)", "Guddu (CC) B-I (Unit 11-13)", "NPPMC - Baloki (OC)"). `canon_plant()` maps any of them
to one canonical name plus separate dims:

    plant    the canonical plant name (title case, no legal suffixes)
    unit     a block / unit / phase within the plant ("Block I, Units 11-13")
    config   CC (combined cycle) or OC (open cycle)
    tranche  a contract tranche of the merit-order price ("up to 152,375 MWh")

Same pattern as `ingestion/geo_pk.py` for districts. Unknown names fall back to a tidied
version of the raw text (and are listed by `unknown_names()` so they can be added here).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Plant:
    name: str
    fuel: str                      # main fuel / technology
    aliases: tuple[str, ...] = field(default_factory=tuple)


def P(name: str, fuel: str, *aliases: str) -> Plant:
    return Plant(name, fuel, tuple(aliases))


PLANTS: list[Plant] = [
    # ---- thermal IPPs (gas / RLNG / furnace oil / coal) ----
    P("Altern Energy", "Gas", "Altern Energy Limited. (AEL)", "ALTERN ENERGY LTD.", "Altern Energy Ltd"),
    P("Atlas Power", "Furnace oil", "Atlas Power Ltd (APL)", "Atlas Power Sheikhupura"),
    P("Attock Gen", "Furnace oil", "Attock Gen Ltd (AGL)", "Attock Gen Morgah"),
    P("Balloki (NPPMCL)", "RLNG", "(NPPCL) - Balloki", "(NPPMCL) - Balloki", "NPPMC - Baloki", "NPPMC-Baloki",
      "RLNG CCPP Balloki"),
    P("Bhikki (QATPL)", "RLNG", "Quaid-e-Azam Thermal Power (Pvt) Ltd. (QATPL)", "QATPL- Bhikki", "QATPL - Bhikki",
      "RLNG CCPP Bhikki"),
    P("China Power Hub", "Imported coal", "China Power Hub Generation Company (Private) Limited. (CPHGCL)",
      "China Power HUB Gen CO", "China Power Hub (1320MW)", "China Power Hub"),
    P("Davis Energen", "Gas", "Davis Energen Power Limited"),
    P("Engro Qadirpur", "Gas", "Engro Powergen Qadirpur Ltd (EPQL)", "Engro PowerGen(Qadir Pur)",
      "Engro PowerGen Qadirpur", "Engro PowerGen"),
    P("Engro Thar", "Local coal", "Engro Powergen Thar (Private) Limited. (EPTPL)", "Engro Power Thar",
      "Engro Thar (660MW)"),
    P("Fauji Kabirwala", "Gas", "Fauji Kabirwala Power Company Limited. (FKPCL)", "FKPCL", "Fauji Kabirwala Power"),
    P("Foundation Power Daharki", "Gas", "Foundation Power Company (Daharki) Ltd", "Foundation Power",
      "Foundation Power Daharki"),
    P("Gulf Powergen", "Furnace oil", "Gulf Powergen (Private) Limited. (GPPL)"),
    P("Habibullah Coastal", "Gas", "Habibullah Coastal Power Company (PVT) Limited. (HCPC)"),
    P("Halmore Power", "RLNG", "Halmore Power Generation Company (Pvt) Ltd. (HPGCL)", "Halmore Power"),
    P("Haveli Bahadur Shah (NPPMCL)", "RLNG", "(NPPCL) - Haveli Bahadar Shah", "(NPPMCL) - Haveli Bahadar Shah",
      "NPPMC-HBS", "NPPMC - HBS", "RLNG CCPP HBS Jhang"),
    P("Hub Power (Hubco)", "Furnace oil", "Hub Power Company (HUBCO)", "HUBCO"),
    P("Japan Power", "Furnace oil", "Japan Power Generation Limited"),
    P("Kohinoor Energy", "Furnace oil", "Kohinoor Energy Limited. (KEL)", "Kohinoor Energy Limited", "KEL"),
    P("Kot Addu (KAPCO)", "Gas", "Kot Addu Power Company (KAPCO)", "KAPCO"),
    P("Lalpir", "Furnace oil", "AES Lalpir power limited", "LALPIR (Pvt) Ltd", "Lalpir Power"),
    P("Liberty Power Daharki", "Gas", "Liberty Power Limited", "LIBERTY Power", "TNB Liberty Power Daharki"),
    P("Liberty Power Tech", "Furnace oil", "Liberty Power Tech Ltd. (LPTL)", "Liberty Power Tech Limited",
      "Liberty Power Tech Faisalabad"),
    P("Lucky Electric", "Imported coal", "Lucky Electric Power Company Limited (LEPCL)", "Lucky Electric Power Limited",
      "Lucky Electric Power Company", "Lucky Electric"),
    P("Narowal (Hubco)", "Furnace oil", "Narowal Energy Ltd", "Narowal Energy Ltd. (HUBCO)", "Narowal Power",
      "HUBCO Narowal"),
    P("Nishat Chunian Power", "Furnace oil", "Nishat Chunian Power Ltd (NCPL)", "Nishat Chunian Power Ltd",
      "Nishat Chunian Power"),
    P("Nishat Power", "Furnace oil", "Nishat Power Ltd (NPL)", "Nishat Power Ltd", "Nishat Power"),
    P("Orient Power", "RLNG", "Orient Power Company (Pvt) Ltd. (OPCL)", "Orient Power Company Ltd",
      "Orient Power Balloki"),
    P("Pakgen", "Furnace oil", "AES Pakgen Power Limited", "PAKGEN Power Ltd.", "PakGen Power"),
    P("Port Qasim Coal", "Imported coal", "Port Qasim Electric Power Company (Private) Limited. (PQEPCL)",
      "PORT QASIM", "Port Qasim (1320MW)"),
    P("Reshma Power", "Furnace oil", "Reshma Power Generation (Private) Limited. (RPGPL)"),
    P("Rousch", "RLNG", "ROUSCH Power Plant", "ROUSCH", "Rousch Power Abdul Hakeem"),
    P("Saba Power", "Furnace oil", "Saba Power Company (Pvt) Ltd", "SABA", "SABA Power Farooqabad"),
    P("Sahiwal Coal", "Imported coal", "Huaneng Shandong Ruyi (Pakistan) Energy (Private) Limited. (HSRPEL)",
      "Sahiwal (1320MW)", "Sahiwal Power"),
    P("Saif Power", "RLNG", "Saif Power Ltd (SPL)", "Saif Power", "Saif Power Qadirabad"),
    P("Sapphire Electric", "RLNG", "Sapphire Electric Company Ltd (SECL)", "Sapphire Electric Co",
      "Sapphire Power Muridke"),
    P("Southern Electric (SEPCOL)", "Furnace oil", "Southern Electric Power Company Limited. (SEPCOL)"),
    P("Thal Nova Thar", "Local coal", "ThalNova Power Thar (Pvt.) Limited (TPTPL)", "Thall NOVA",
      "CFPP Thal Nova Thar"),
    P("Thar Coal Block-I", "Local coal", "Thar Coal Block-I Power Generation Company (Private) Limited (TCB-I)",
      "Thar Coal Block-I", "1320MW TCB-I Tharparkar"),
    P("Thar Energy", "Local coal", "Thar Energy Limited (TEL)", "Thar Energy Limited"),
    P("Trimmu (Punjab Thermal)", "RLNG", "Punjab Thermal Power (Pvt) Ltd", "Punjab Thermal Power",
      "RLNG CCPP Head Trimu"),
    P("UCH", "Gas", "UCH Power Plant", "UCH", "UCH Power - I"),
    P("UCH-II", "Gas", "UCH-II Power (Pvt) Ltd", "UCH-II", "UCH Power - II"),
    # ---- public-sector thermal (GENCOs) ----
    P("Faisalabad GTPS", "Gas", "GTPS Faisalabad"),
    P("Faisalabad SPS", "Gas", "SPS Faisalabad"),
    P("Guddu", "Gas", "Thermal Power Station Guddu", "Guddu", "TPS Guddu"),
    P("Guddu 747", "Gas", "Thermal Power Station Guddu 747", "747 MW GUDDU", "TPS Guddu 747MW"),
    P("Jamshoro", "Furnace oil", "Jamshoro Power Generation Company Limited", "Jamshoro"),
    P("Jamshoro Coal", "Imported coal", "Jamshoro (Coal)", "660MW CFPP Jamshoro"),
    P("Kotri", "Gas", "Kotri Power Station"),
    P("Lakhra", "Local coal", "Lakhra Power Generation Company Limited"),
    P("Muzaffargarh", "Furnace oil", "Thermal Power Station Muzaffargarh", "Muzaffargarh"),
    P("Nandipur", "RLNG", "Nandipur Power Plant", "Nandipur", "Nandipur TPS (425 MW)"),
    P("Quetta TPS", "Gas", "Thermal Power Station Quetta"),
    # ---- nuclear ----
    P("Chashma Nuclear C-1", "Nuclear", "Chashma Nuclear Power Plant 1 (CHASHNUPP-I)", "CHASHNUPP - C1"),
    P("Chashma Nuclear C-2", "Nuclear", "Chashma Nuclear Power Plant 2 (CHASHNUPP-II)", "CHASHNUPP - C2"),
    P("Chashma Nuclear C-3", "Nuclear", "Chashma Nuclear Power Plant 3 (CHASHNUPP-III)", "CHASHNUPP - C3"),
    P("Chashma Nuclear C-4", "Nuclear", "Chashma Nuclear Power Plant 4 (CHASHNUPP-IV)", "CHASHNUPP - C4"),
    P("Karachi Nuclear K-2", "Nuclear", "Karachi Nuclear Power Plant-II (K-2)", "KANUPP - K2"),
    P("Karachi Nuclear K-3", "Nuclear", "Karachi Nuclear Power Plant-III (K-3)", "KANUPP - K3"),
    # ---- bagasse ----
    P("Al-Moiz Industries", "Bagasse", "Al-Moiz Industries Limited. (AMIL)", "Al-Moiz Industries"),
    P("Chanar Energy", "Bagasse", "Chanar Energy Limited. (CEL)", "Chanaar Energy Limited"),
    P("Chiniot Power", "Bagasse", "Chiniot Power Limited. (CPL)", "Chiniot Sugar Mill"),
    P("Fatima Energy", "Bagasse", "Fatima Energy Limited. (FEL)"),
    P("Hamza Sugar Mills", "Bagasse", "Hamza Sugar Mills Limited. (HSML)", "Hamza Sugar Mills (15 MW) RYK"),
    P("JDW Unit-II Sadiqabad", "Bagasse", "JDW Sugar Mills Limited. (Unit-II) Rahim Yar Khan", "JDW-II Cogen Sadiqabad"),
    P("JDW Unit-III Ghotki", "Bagasse", "JDW Sugar Mills Limited. (Unit-III) Ghotki", "JDW-III Cogen Ghotki"),
    P("Layyah Sugar Mills (Thal Industries)", "Bagasse",
      "Thal Industries Corporation Limited. (TICL) - Layyah Sugar Mills", "Layyah Sugar Mill"),
    P("RYK Mills", "Bagasse", "RYK Mills Limited. (RYKML)", "RYK Sugar Mill (30MW)"),
    P("Shahtaj Sugar Mills", "Bagasse", "32MW (Bagasse) Shahtaj Sugar Mills Mandi Bahauddin"),
    # ---- solar ----
    P("AJ Power", "Solar", "AJ Power"),
    P("Apollo Solar", "Solar", "Apollo Solar Development Pakistan Limited", "Appolo Bahawalpur"),
    P("Atlas Solar (ex-Zhenfa)", "Solar",
      "Atlas Solar Limited (Formerly Zhenfa Pakistan New Energy Company Private Limited)",
      "Zhenfa Pakistan New Energy Limited", "Zhenfa"),
    P("Best Green Energy", "Solar", "Best Green Energy Pakistan Limited. (BGEPL)", "Best Green Bahawalpur"),
    P("Crest Energy", "Solar", "Crest Energy Pakistan Limited", "Crest Energy Bahawalpur"),
    P("Harappa Solar", "Solar", "Harappa 18MWp"),
    P("Helios Power", "Solar", "Helios Power (Private) Limited (HPPL)", "50MW Helios Sukkur"),
    P("HNDS Energy", "Solar", "HNDS Energy (Private) Limited (HEPL)", "50MW HNDS Sukkur"),
    P("Meridian Energy", "Solar", "Meridian Energy (Private) Limited (MEPL)", "50MW Meridian Sukkur"),
    P("Quaid-e-Azam Solar", "Solar", "Quaid-e-Azam Solar Power (Private) Limited. (QASPL)", "QuaideAzam Bahawalpur"),
    # ---- wind ----
    P("ACT Wind (ex-Tapal)", "Wind", "Tapal Wind Power Energy Private Ltd. (TWEPL)", "ACT Wind (ex-TAPAL)"),
    P("ACT Wind 2 (ex-Tapal II)", "Wind", "Tapal Wind - II", "Tapal Wind Power Energy Private Ltd. - 2 (TWEPL-II)",
      "ACT 2 Wind"),
    P("Artistic Wind", "Wind", "Artistic Wind Power (Pvt.) Limited", "Artistic Energy (AEPL)"),
    P("Artistic Wind II", "Wind", "Artistic Wind - II", "Artistic Wind Power (Pvt.) Limited. - II",
      "Artistic [2] Wind Power"),
    P("Din Energy", "Wind", "Din Energy Limited", "Din Wind Energy Ltd", "Din Energy"),
    P("FFC Energy", "Wind", "FFC Energy Ltd", "FFCEL"),
    P("Foundation Wind I", "Wind", "Foundation Wind Energy-I Ltd. (FWEL-I)", "FWEL-I"),
    P("Foundation Wind II", "Wind", "Foundation Wind Energy-I Ltd. (FWEL-II)", "FWEL-II"),
    P("Gul Ahmed Wind", "Wind", "Gul Ahmed Wind Power Ltd", "Gul Ahmad"),
    P("Gul Ahmed Electric", "Wind", "Gul Ahmed Electric Limited", "Gul Ahmed Wind Power Ltd. - II", "Gul Ahmad 2"),
    P("Hawa Energy", "Wind", "Hawa Energy (Private) Ltd. (HEPL)", "Hawa Energy"),
    P("Hydrochina Dawood", "Wind", "Hydrochina Dawood Power (Pvt.) Ltd. (HDPPL)", "Hydro China Dawood"),
    P("Indus Wind Energy", "Wind", "Indus Wind Energy Limited"),
    P("Jhimpir Power", "Wind", "Jhimpir Power (Private) Limited. (JPPL)", "Jhampir Power"),
    P("Lakeside Energy", "Wind", "Lakeside Energy (Pvt.) Limited", "Lake Side Energy Pvt Ltd", "Lakeside Energy"),
    P("Liberty Wind I", "Wind", "Liberty Wind Power - 1 (Pvt.) Limited", "Liberty Wind Power I (Pvt.) Limited",
      "Liberty-1"),
    P("Liberty Wind II", "Wind", "Liberty Wind Power 2 (Pvt.) Limited", "Liberty Wind Power - II", "Liberty-2"),
    P("Lucky Renewables (ex-Tricom)", "Wind",
      "Lucky Renewables (Pvt.) Limited (Tricom Wind Power (Pvt.) Limited)", "Tricom Wind Power (Pvt.) Limited",
      "Lucky Renewables (TRICOM)"),
    P("Master Green Energy", "Wind", "Master Green Energy Limited", "Master Green"),
    P("Master Wind Energy", "Wind", "Master Wind Energy Limited. (MWEL)", "Master Energy"),
    P("Metro Power", "Wind", "Metro Power Company Ltd. (MPCL)", "Metro Power"),
    P("Metro Wind II", "Wind", "Metro Power Company Ltd. (MPCL) - II", "Metro Wind Power Limited - II (MWPL - II)",
      "Metro 2"),
    P("NASDA Green Energy", "Wind", "NASDA Green Energy (Pvt.) Limited", "NASDA"),
    P("Sachal Energy", "Wind", "Sachal Energy Development (Pvt) Ltd. (SEDPL)", "Sachal Energy"),
    P("Sapphire Wind", "Wind", "Sapphire Wind Power Company Ltd. (SWPCL)", "Sapphire"),
    P("Tenaga Generasi", "Wind", "Tenaga Generasi Ltd", "Tenaga Generasi"),
    P("Three Gorges First Wind", "Wind", "Three Gorges First Wind Farm Pakistan (Pvt.) Ltd. (TGF)", "Three Gorges First"),
    P("Three Gorges Second Wind", "Wind", "Three Gorges Second Wind Farm (Private) Ltd. (TGS)", "Three Gorges Second"),
    P("Three Gorges Third Wind", "Wind", "Three Gorges Third Wind Farm (Private) Ltd. (TGT)", "Three Gorges Third"),
    P("Tricon Boston A", "Wind", "Tricon Boston Consulting Corporation (Private) Ltd-A (TBCCPL-A)", "Tricon Boston - A"),
    P("Tricon Boston B", "Wind", "Tricon Boston Consulting Corporation (Private) Ltd-B (TBCCPL-B)", "Tricon Boston - B"),
    P("Tricon Boston C", "Wind", "Tricon Boston Consulting Corporation (Private) Ltd-C (TBCCPL-C)", "Tricon Boston - C"),
    P("UEP Wind", "Wind", "UEP Wind Power (Pvt) Ltd", "United Energy Pakistan"),
    P("Yunus Energy", "Wind", "Yunus Energy Ltd", "Yunus Energy"),
    P("Zephyr Power", "Wind", "Zephyr Power (Pvt.) Ltd", "Zephyr Power"),
    P("Zorlu Enerji", "Wind", "Zorlu Enerji Pakistan Ltd. (ZEPL)", "Zorlu Enerji"),
    # ---- hydropower ----
    P("Allai Khwar", "Hydel", "Allai Khwar Hydropower Project (WAPDA)"),
    P("Allai, Dubair and Khan Khwar + Ranolia", "Hydel", "HPS (Allai+Dubair+Khan Khawar) + Ranolia"),
    P("Chashma Hydro", "Hydel", "Chashma Hydropower Project (WAPDA)", "HPS WAPDA Chashma"),
    P("Chichoki Mallian", "Hydel", "HPS WAPDA Chicho ki Malian"),
    P("Chitral", "Hydel", "HPS WAPDA Chitral"),
    P("Daral Khwar", "Hydel", "Daral Khwar (Behren) Hydropower Project", "HPS PEDO Daral Khawar"),
    P("Dargai", "Hydel", "HPS WAPDA Dargai"),
    P("Dubair Khwar", "Hydel", "Dubair Khwar Hydropower Project (WAPDA)"),
    P("Ghazi Barotha", "Hydel", "Ghazi Brotha Hydropower Project (WAPDA)", "HPS WAPDA Ghazi Barotha"),
    P("Golen Gol", "Hydel", "Golen Gol Hydropower Project (WAPDA)", "HPS WAPDA Golen Gol"),
    P("Gomal Zam", "Hydel", "HPS WAPDA Gomal Zam"),
    P("Gulpur", "Hydel", "Gulpur Hydropower Project", "HPS Gulpir Kotli"),
    P("Head Marala", "Hydel", "HPS Head Marala 7.64MW"),
    P("Jabban", "Hydel", "HPS WAPDA Jabban"),
    P("Jabori", "Hydel", "HPS Jabori"),
    P("Jagran", "Hydel", "Jagran Hydropower Project", "HPS Jagraan Hydropower AJK"),
    P("Jinnah Hydro", "Hydel", "Jinnah Hydel Power Project (WAPDA)", "HPS WAPDA Jinnah"),
    P("Karot", "Hydel", "Karot HydroPower Project", "Karot Power Company (Pvt.) Ltd. (KPCL)", "HPS Karot 720MW"),
    P("Khan Khwar", "Hydel", "Khan Khwar Hydropower Project (WAPDA)"),
    P("Kurram Garhi", "Hydel", "HPS WAPDA Kurram Garhi"),
    P("Machai", "Hydel", "HPS PEDO Machai"),
    P("Malakand-III", "Hydel", "Malakand - III Hydropower Project", "HPS PEDO Malakand-III"),
    P("Mangla", "Hydel", "Mangla Hydropower Project (WAPDA)", "HPS WAPDA Mangla"),
    P("Nandipur Hydro", "Hydel", "HPS WAPDA Nandipur"),
    P("Neelum-Jhelum", "Hydel", "Neelum Jhelum Hydropower Project (WAPDA)", "HPS Neelum Jhelum"),
    P("New Bong Escape (Laraib)", "Hydel", "Laraib Hydropower Project / New Bong Escape",
      "New Bong Escape Hydropower Project", "HPS New Bong (Laraib Energy)"),
    P("Patrind", "Hydel", "Patrind Hydropower Project", "HPS Patrind 147MW"),
    P("Pehur", "Hydel", "HPS PEDO Pehur"),
    P("Ranolia", "Hydel", "HPS Ranolia"),
    P("Rasul", "Hydel", "HPS WAPDA Rasul"),
    P("Renala", "Hydel", "HPS WAPDA Renala"),
    P("Shadiwal", "Hydel", "HPS WAPDA Shadiwal"),
    P("Suki Kinari", "Hydel", "Suki Kinari Hydropower Project", "884MW HPS Suki Kinari"),
    P("Tarbela", "Hydel", "Tarbela Hydropower Project (WAPDA)", "HPS WAPDA Tarbela"),
    P("Tarbela 4th Extension", "Hydel", "Tarbela Ext. 04 Hydropower Project (WAPDA)"),
    P("Warsak", "Hydel", "Warsak Hydropower Project (WAPDA)", "HPS WAPDA Warsak"),
]

DISCOS = {  # canonical spellings of the distribution companies
    "fesco": "FESCO", "gepco": "GEPCO", "hesco": "HESCO", "iesco": "IESCO", "lesco": "LESCO",
    "mepco": "MEPCO", "pesco": "PESCO", "qesco": "QESCO", "sepco": "SEPCO", "tesco": "TESCO",
    "kelectric": "K-Electric", "ke": "K-Electric", "kesc": "K-Electric",
}

_ROMAN = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5"}


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_BY_KEY: dict[str, Plant] = {}
for _p in PLANTS:
    for _n in (_p.name, *_p.aliases):
        k = _key(_n)
        if k in _BY_KEY and _BY_KEY[k] is not _p:
            raise ValueError(f"plant alias {_n!r} is claimed by {_BY_KEY[k].name} and {_p.name}")
        _BY_KEY[k] = _p

_seen_unknown: set[str] = set()


def _roman(s: str) -> str:
    return " ".join(_ROMAN.get(t, t) for t in s.split())


def split_details(raw: str) -> tuple[str, dict[str, str]]:
    """Pull tranche / CC-OC / block-unit-phase details out of a plant label."""
    s = re.sub(r"\s+", " ", raw).strip()
    extra: dict[str, str] = {}
    m = re.search(r"\(\s*(up\s*to|upto|above)\s*([\d,]+)\s*MWh\s*\)", s, re.I)
    if m:
        extra["tranche"] = f"{'up to' if m.group(1).lower().replace(' ', '') == 'upto' else 'above'} {m.group(2)} MWh"
        s = (s[:m.start()] + s[m.end():]).strip()
    m = re.search(r"\(\s*(CC|OC)\s*\)", s)
    if m:
        extra["config"] = m.group(1)
        s = (s[:m.start()] + " " + s[m.end():]).strip()
    s = re.sub(r"\(\s*HSD\s*\)", "", s).strip()               # the fuel dim already says diesel
    parts = []
    m = re.search(r"\bB-(I{1,3}|IV)\b", s)
    if m:
        parts.append(f"Block {m.group(1)}")
        s = (s[:m.start()] + s[m.end():]).strip()
    m = re.search(r"\bBlock-(I{1,3}|IV)\b", s)
    if m:
        parts.append(f"Block {m.group(1)}")
        s = (s[:m.start()] + s[m.end():]).strip()
    m = re.search(r"\(\s*Unit\s*-?\s*([\d\- ]+)\)", s, re.I)
    if m:
        u = re.sub(r"\s+", "", m.group(1)).strip("-")
        parts.append(f"{'Units' if '-' in u else 'Unit'} {u}")
        s = (s[:m.start()] + s[m.end():]).strip()
    m = re.search(r"\(\s*Ph-(I{1,3})\s*\)", s)
    if m:
        parts.append(f"Phase {m.group(1)}")
        s = (s[:m.start()] + s[m.end():]).strip()
    m = re.match(r"^(Thermal Power Station Guddu) (\d+-\d+)$", s)   # NEPRA: Guddu 1-4 / 5-13
    if m:
        parts.append(f"Units {m.group(2)}")
        s = m.group(1)
    if parts:
        extra["unit"] = ", ".join(parts)
    return re.sub(r"\s+", " ", s).strip(" -"), extra


_LEGAL = re.compile(r"\b(private|pvt|ltd|limited|company|co|plc|the)\b\.?", re.I)


def tidy(name: str) -> str:
    """Fallback for names not in the registry: drop legal suffixes and acronym tails."""
    s = re.sub(r"\((?:[A-Z][A-Z0-9\-\s]{1,14})\)\s*$", "", name).strip()
    s = _LEGAL.sub("", s)
    s = re.sub(r"\(\s*\)|\s+\.", "", s)
    return re.sub(r"\s+", " ", s).strip(" .,-")


# second index ignoring legal suffixes ("… (Pvt.) Ltd", "… Limited. (XYZ)")
_BY_TIDY: dict[str, Plant] = {}
for _p in PLANTS:
    for _n in (_p.name, *_p.aliases):
        _BY_TIDY.setdefault(_key(tidy(_n)), _p)


def canon_plant(raw: str) -> dict[str, str]:
    """{"plant": canonical name, + unit / config / tranche when present}."""
    whole = _BY_KEY.get(_key(raw))           # a registry name like "Thar Coal Block-I" stays whole
    if whole is not None:
        return {"plant": whole.name}
    base, extra = split_details(raw)
    p = _BY_KEY.get(_key(base)) or _BY_TIDY.get(_key(tidy(base)))
    if p is None:
        _seen_unknown.add(raw)
        return {"plant": tidy(base), **extra}
    return {"plant": p.name, **extra}


def canon_disco(raw: str) -> str:
    return DISCOS.get(_key(raw), raw.strip())


def unknown_names() -> list[str]:
    return sorted(_seen_unknown)


def plant_fuel(name: str) -> str | None:
    p = _BY_KEY.get(_key(name))
    return p.fuel if p else None
