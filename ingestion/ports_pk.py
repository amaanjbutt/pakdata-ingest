"""One registry of names for the ports data (Port Qasim, Karachi Port): commodities, terminals, shipping
agents and countries, as the daily statements spell them.

The statements are typed by hand each morning, so one thing has many spellings ("SOYABEEN OIL",
"CONDENSENT", "RICHARD BEY", "INDONASIA"), the anchorage table cuts agents at 16 letters ("EASTWIND
SHIPPIN") and lets the cut-off letter run into the flag ("EPANAMA"), and publisher abbreviations ("L.N.G",
"H.S.F.O", "PHOSP. ACID", "M.ISLAND", "FOTCO") must never reach the site. Each function maps raw text to
one readable name; the raw text is kept in its own column. Built from every spelling in PQA's statements
from May 2019 to October 2026. Unknown text falls back to a tidied version of itself (countries: to
nothing, so a port name never poses as a country) — add new spellings here.

    unit_of(text)            'M.Ton' -> 'metric ton', 'C.Met' -> 'cubic metre', else None
    commodity(raw, unit)     -> (readable commodity, unit of its figures)
    terminal(berth)          'QICT-VII' -> 'Qasim International Container Terminal (QICT)'
    kpt_terminal(berth, wharf)  Karachi Port berth / container terminal -> terminal or wharf
    agent(raw)               'EASTWIND SHIPPIN' -> 'Eastwind Shipping'
    country(raw)             flag or port -> country ('M.ISLAND' -> 'Marshall Islands', 'RICHARD BEY' ->
                             'South Africa'); None for Pakistan's own ports ('PQA') and unknown text
"""
from __future__ import annotations

import re

UNITS = {
    "metric ton": ("M.TON", "M.TONS", "M TON", "MTON", "M/T", "MT", "M.T", "TON", "TONS", "TONNE", "TONNES",
                   "M.TONNES", "METRIC TON", "M/TON"),
    "cubic metre": ("C.MET", "C.METER", "C.METRE", "C.MTR", "CMET", "C MET", "CBM", "CU.M", "M3"),
    "units": ("NOS", "NOS.", "NO.", "UNITS", "UNIT", "VEHICLES", "PCS"),
    "teu": ("TEU", "TEUS", "TUE", "TUES"),
}
_UNIT = {re.sub(r"[\s]", "", v): k for k, vs in UNITS.items() for v in vs}


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()


def unit_of(text: str) -> str | None:
    """The unit a commodity cell's unit line names, or None if the line isn't a unit. Pure."""
    t = re.sub(r"\s", "", (text or "").upper())
    return _UNIT.get(t) or _UNIT.get(t.rstrip("."))


_SMALL = {"AND", "OF", "THE", "FOR", "IN"}


def tidy(text: str) -> str:
    """'AL-PINE MARINE' -> 'Al-Pine Marine'; keeps short all-capital tokens (LNG, NYK, MSC). Pure."""
    out = []
    for w in re.sub(r"\s+", " ", (text or "").strip()).split(" "):
        bare = re.sub(r"[^A-Za-z]", "", w)
        if len(bare) <= 3 and bare.isupper() and bare not in _SMALL | {"OIL", "SEA", "AGE", "PAK", "BAY"}:
            out.append(w)
        elif bare.upper() in _SMALL and out:
            out.append(w.lower())
        else:
            out.append("-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
    return " ".join(out).strip()


# ---- commodities -------------------------------------------------------------------------------------------
# readable name -> raw spellings (compared on letters and digits only, upper case)
COMMODITIES: dict[str, tuple[str, ...]] = {
    "Containers": ("CONTAINERS", "CONTAINER", "CONTS", "ONTAINERS D L", "CONTAINERS D L"),
    "Coal": ("COAL",),
    "Petroleum coke": ("PET COKE", "PETCOKE", "PETROLEUM COKE", "COKE"),
    "Metallurgical coke": ("MET COKE",),
    "Coke breeze": ("COKE BREEZ", "COKE BREEZE"),
    "Pig iron and coke": ("PIG IRON COKE",),
    "LNG": ("L N G", "LNG", "R L N G", "RLNG"),
    "LPG": ("L P G", "LPG", "PROPANE", "BUTANE"),
    "Crude oil": ("CRUDE OIL", "CRUDE"),
    "Condensate": ("CONDENSATE", "CONDENSER", "CONDENSENT", "CONDEN SATE", "CONDIASTY", "CODENSATE"),
    "Petrol": ("MOGAS", "GASOLLENE", "GASOLINE", "PETROL", "UMS RON92", "RON92", "MOTOR SPIRIT"),
    "Diesel (gas oil)": ("GAS OIL", "GASOIL", "H S D O", "H S D", "HSD", "HSDO", "ULSD", "DIESEL", "HSD OIL"),
    "Furnace oil (HSFO)": ("H S F O", "HSFO", "FURNACE OIL", "FURANCE OIL", "FUIL OIL", "FUEL OIL", "HIGH SULPHER FURNACE OIL H S F O",
                           "HIGH SULPHUR FURNACE OIL"),
    "Low-sulphur fuel oil": ("LSFO", "VLSF OIL", "VLSFO"),
    "Naphtha": ("NAPHTHA", "NAPTHA"),
    "Jet fuel": ("JET FUEL", "JP 1", "JP1", "JET A 1"),
    "Bitumen": ("BITUMEN", "PETROLIUM BITRMEN", "PETROLEUM BITUMEN", "ASPHALT"),
    "Palm oil": ("PALM OIL", "RBDBO", "RBD PALM OIL", "CPO"),
    "Palm olein": ("PALM OLINE", "PALM OLEIN", "PALMOLEIN", "PALMLENE", "PALMLENE SEED"),
    "Palm stearin": ("PALM STEARIN", "PALM STEELINE", "SOFT STEELINE"),
    "Palm kernel": ("PALM KERNEL", "PALM KERNEL BULK", "PALM KERNEL EXPELLER"),
    "Soybean oil": ("SOYABEEN OIL", "SOYABEAN OIL", "SOYA BEAN OIL", "SOYBEAN OIL"),
    "Canola oil": ("CANOLA OIL",),
    "Rapeseed oil": ("RAPE SEED OIL", "RAPESEED OIL"),
    "Sunflower oil": ("SUNFLOWER OIL",),
    "Vegetable oil": ("VEGETABLE OIL", "EDIBLE OIL"),
    "Soybeans": ("SOYABEAN SEED", "SOYABEAN", "SOYA BEAN SEED", "SOYA SEED", "SOYA BEAN", "SOYBEANS"),
    "Soybean meal": ("SOYA SEED MEAL", "SOYABEAN MEAL", "SOYA MEAL"),
    "Soybeans and sunflower seed": ("S BEAN S FLOWE",),
    "Canola seed": ("CANOLASEED", "CANOLA SEED", "GREEN CANOLA", "CANOLA"),
    "Rapeseed": ("RAPE SEED", "RAPESEED"),
    "Sunflower seed": ("SUNFLOWER SEED",),
    "Wheat": ("WHEAT",),
    "Maize (corn)": ("CORN", "MAIZE"),
    "Rice": ("RICE",),
    "Sugar": ("SUGAR",),
    "Molasses": ("MOLASSES",),
    "Chickpeas": ("CHICKPEAS", "CHICK PEA", "CHICK PEAS", "GRAM"),
    "Yellow peas": ("YELLOWPEAS", "YELLOW PEAS"),
    "Peas": ("PEAS",),
    "Lentils": ("LENTIL", "LENTILS", "LENTIL PEAS"),
    "Animal feed": ("ANIMAL FEED",),
    "Cement": ("CEMENT",),
    "Clinker": ("CLINKER", "CLINKERS"),
    "Cement and rice": ("CEMENT RICE",),
    "Cement and clinker": ("CEMENT CLINKERS", "CEMENT CLINKER"),
    "Steel coils": ("STEEL COIL", "STEEL COILS", "HR COIL", "CR COIL"),
    "Steel billets": ("STEEL BILLETS", "BILLETS"),
    "Steel plates": ("STEEL PLATE", "STEEL PLATES"),
    "Steel pipes": ("STEEL PIPE", "STEEL PIPES", "PIPES"),
    "Steel products": ("STEEL PRODUCT", "STEEL PRODUCTS", "STEEL"),
    "Iron ore": ("IRON ORE",),
    "Furnace slag": ("FOUNACE SLAUG", "FURNACE SLAG", "SLAG", "SLAGS", "STAGS"),
    "Fertilizer": ("FERTILIZER", "FERTILISER"),
    "Urea fertilizer": ("UREA", "FERTILIZER UREA"),
    "DAP fertilizer": ("FERTILIZER DAP", "D A PHOSPHAT E", "DAP", "DI AMMONIUM PHOSPHATE DAP", "DI AMMONIUM PHOSPHATE"),
    "Ammonium nitrate fertilizer": ("AMMONIUM NITRATE FER", "AMMONIUM NITRATE"),
    "Potash": ("POTASH",),
    "Rock phosphate": ("ROCK PHOSPHATE",),
    "Phosphoric acid": ("PHOS ACID", "PHOSPHORIC ACID", "PHOSP ACID"),
    "Sulphate": ("SULPHATE",),
    "Sodium sulphate": ("SUDIUM SULPHATE", "SODIUM SULPHATE"),
    "Paraxylene": ("PARAXYLENE", "PARA XYLENE"),
    "Purified terephthalic acid (PTA)": ("PTA BAGS", "PURIFIED TEREPH ACID", "PTA"),
    "Monoethylene glycol (MEG)": ("M E G", "MEG"),
    "Ethylene": ("ETHYLENE", "ATHYLENE"),
    "Ethylene dichloride (EDC)": ("EDC",),
    "Vinyl chloride monomer (VCM)": ("V C M", "VCM"),
    "Vinylidene fluoride": ("VENE FLORIDE",),
    "Styrene monomer": ("STYRENE MONOMER",),
    "Methanol": ("METHANOL",),
    "Acetic acid": ("ACETIC ACID",),
    "Ethanol": ("ETHANOL",),
    "Chemicals": ("CHEMICALS", "CHEMICAL"),
    "Polymers": ("POLYMERS", "POLYMER"),
    "Polyester chips": ("POLYSITAR CHIPS", "POLYESTER CHIPS"),
    "Talc": ("TALCUM POWDER", "TALC POWDER", "TALC"),
    "Bentonite": ("BENTONITE",),
    "Clay": ("CLAY IN BAGS", "CLAY"),
    "Sand": ("SAND", "RIVER SAND"),
    "Crushed stone": ("CRASHED STONE", "CRUSHED STONE"),
    "Wood pulp": ("WOOD PULP",),
    "Wind turbine equipment": ("WIND POWER", "WIND MILL", "WIND MILLS", "WIND TOWER", "ROUTER BLADE", "D ROUTER BELT"),
    "General cargo": ("GENERAL CARGO", "GEN CARGO"),
    "Oilseeds": ("SEEDS",),
    "Oilseeds and general cargo": ("SEEDS GEN CARGO",),
    "Steel plates and billets": ("STEEL PLAT OR BILLTES",),
    "Steel mill raw materials": ("RAW MATERIAL PAKSTEEL", "RAW MATERIAL IMPORTS PAKSTEEL", "RAW MATERIAL"),
    "Other cargo": ("OTHERS", "OTHER"),
    "Project cargo": ("PROJECT CARGO", "CEMENTED BEAM", "SPARE PARTS", "CRANES", "FLOATING CREAN"),
    "Project and general cargo": ("P G CARGO", "PG CARGO"),
    "Vehicles (ro-ro)": ("RO RO", "RORO", "VEHICLES", "CARS"),
    "Dredging": ("DREDGER", "DREDGING PIPE", "EQUIPMENT FOR DREDGE"),
    "Bunkering": ("BUNKER SUPPLY", "BUNKER REPAIR"),
    "Barge": ("BARGE",),
}
_COMMODITY = {_key(v): k for k, vs in COMMODITIES.items() for v in vs} | {_key(k): k for k in COMMODITIES}
# text that isn't a commodity: "NIL", and a few country names a shifted cell put in the column
_NOT_COMMODITY = {"NIL", "NILL", "M ISLAND", "PANAMA", "HONGKONG", "DJIBOUTI"}
# commodities whose figures are counted in their own unit whatever the unit line says
_FIXED_UNIT = {"Containers": "teu", "LNG": "cubic metre"}  # PQA's container figures are TEUs (they sum to its annual TEU table)
_LEAD_OP = re.compile(r"^(?:DIS(?:C(?:H)?)?|LOAD(?:ING)?|DISCHARGE)\s+(?:LOAD\s+)?")


def commodity(raw: str, unit_raw: str = "") -> tuple[str | None, str | None]:
    """(readable commodity, unit of its figures). Pure."""
    k = _key(raw)
    if not k or k in _NOT_COMMODITY or k.isdigit():
        return None, unit_of(unit_raw)
    name = _COMMODITY.get(k) or _COMMODITY.get(_LEAD_OP.sub("", k)) or tidy(raw).capitalize()
    unit = _FIXED_UNIT.get(name) or unit_of(unit_raw) or "metric ton"
    return name, unit


# ---- terminals ---------------------------------------------------------------------------------------------
TERMINALS: dict[str, tuple[str, ...]] = {
    "Marginal Wharves (multi-purpose)": ("M W", "MW", "MPT"),
    "Qasim International Container Terminal (QICT)": ("QICT",),
    "QICT-2 Container Terminal": ("QT2", "QT 2", "QICT 2"),
    "Pakistan International Bulk Terminal (PIBT)": ("PIBT",),
    "Liquid Cargo Terminal (LCT)": ("LCT",),
    "Fauji Akbar Portia Grain & Fertilizer Terminal (FAP)": ("FAP",),
    "Fauji Oil Terminal (FOTCO)": ("FOTCO",),
    "Engro Vopak Terminal (EVTL)": ("EVTL",),
    "Iron Ore & Coal Berth (IOCB)": ("IOCB",),
    "SSGC LPG Terminal": ("SSGC",),
    "Progas LPG Terminal": ("PROGAS",),
    "HFP&S Coal Terminal": ("HFP S", "HFPS", "HFP"),
    "Engro Elengy LNG Terminal (EETL)": ("EETL", "EEPTL"),
    "Pakistan GasPort LNG Terminal (PGPCL)": ("PGPCL", "PGP"),
    "Port Qasim Electric Power Coal Terminal (PQEPT)": ("PQEPT",),
    "Other berths": ("OTHER",),
    "Outer anchorage": ("O ANCH", "OUTER ANCH", "OUTER ANCHORAGE", "OPL"),
}
_TERMINAL = {_key(v): k for k, vs in TERMINALS.items() for v in vs}


def terminal(berth: str) -> str | None:
    """PQA berth or terminal code ('QICT-VII', 'M/W-II', 'HFP&S-IV', 'QT-2') -> the terminal's readable
    name; numbers (a shifted cell) -> None. Pure."""
    k = _key(berth)
    if not k or re.fullmatch(r"[\d ]+", k):
        return None
    for cand in (k, re.sub(r"\s+(?:[IVX]+|\d+)$", "", k)):
        if cand in _TERMINAL:
            return _TERMINAL[cand]
    return tidy(berth)


def terminal_in(label: str) -> str | None:
    """The PQA terminal a heading names ('FOTCO- FAUJI OIL TERMINAL …', 'LPG TERMINAL - SSGC- …',
    'HFP&S- HUANENG …'), from its first token that is a terminal code; None if none is. Pure."""
    for tok in re.split(r"[\s\-–(),.]+", label or ""):
        k = _key(tok)
        if k and k in _TERMINAL:
            return _TERMINAL[k]
    return None


KPT_TERMINALS: dict[str, tuple[str, ...]] = {
    "South Asia Pakistan Terminals (SAPT)": ("SAPT", "SAPTL"),
    "Karachi International Container Terminal (KICT)": ("KICT",),
    "Pakistan International Container Terminal (PICT)": ("PICT",),
    "Oil Piers": ("OP",),
    "Napier Mole Boat Wharf (NMB)": ("NMB",),
    "Karachi Gateway Terminal (KGTL)": ("KGTL",),
}
_KPT_TERMINAL = {_key(v): k for k, vs in KPT_TERMINALS.items() for v in vs}


def kpt_terminal(berth: str, wharf: str | None) -> str | None:
    """Karachi Port berth ('Sapt-1', 'OP-1', 'Nmb-2', 'B-6/B-7') or container terminal code ('SAPTL') ->
    its terminal; otherwise the statement's wharf heading ('East Wharf'). Pure."""
    k = _key(berth)
    for cand in (k, re.sub(r"\s*\d+.*$", "", k)):
        if cand in _KPT_TERMINAL:
            return _KPT_TERMINAL[cand]
    return tidy(wharf) if wharf else (tidy(berth) or None)


def kpt_vessel_type(work: str) -> str | None:
    """What a Karachi Port ship carries, broadly, from its work text. Pure."""
    k = _key(work)
    if "CONTAINER" in k:
        return "container"
    if re.search(r"\b(OIL|CRUDE|DIESEL|HSD|HSFO|MOGAS|GASOLINE|PETROL|NAPHTHA|JET|LPG|LNG|CHEMICAL|ETHANOL|"
                 r"MOLASSES|ACID|FUEL)\b", k):
        return "tanker"
    return "bulk_or_general" if k else None


# ---- shipping agents ---------------------------------------------------------------------------------------
AGENTS: dict[str, tuple[str, ...]] = {
    "Alpine Marine Services": ("AL PINE MARINE", "ALPINE MARINE", "ALPINE MARINE SERVICES"),
    "GAC Shipping": ("GAC SHIPPING", "GAC"),
    "MSC Pakistan": ("MSC PAKISTAN", "MSC AGENCY PAKISTAN"),
    "Maersk Pakistan": ("MAERSK PAK", "MAERSK PAKISTAN"),
    "Ocean World": ("OCEAN WORLD",),
    "Ocean Services": ("OCEAN SERVICES",),
    "GSA": ("GSA",),
    "M. International": ("M INTERNATIONAL",),
    "Transmarine": ("TRANSMARINE", "TRANS MARINE"),
    "Eastwind Shipping": ("EASTWIND SHIPPING",),
    "CMA CGM Pakistan": ("CMA CGM PAKISTAN",),
    "Wilhelmsen Ships Service": ("WILHELMSEN SHIPS", "WILHELMSEN SHIP", "WILHELMSEN SHIPS SERVICE"),
    "COSCO Saeed": ("COSCO SAEED",),
    "Global Maritime": ("GLOBAL MARITIME",),
    "Trade2Shore": ("TRADE2SHORE", "T2S"),
    "Ocean Network Express": ("OCEAN NETWORK EXPRESS", "OCEAN NETWORK", "ONE", "OCEAN NETWORK EXPRESS PAKISTAN"),
    "Asia Marine": ("ASIA MARINE",),
    "Hapag-Lloyd Pakistan": ("HAPAG LIOYD PAKISTAN", "HAPAG LLOYD PAKISTAN"),
    "Sinotrans Logistics": ("SINOTRANS LOGISTICS",),
    "X-Press Feeders": ("X PRESS FEEDERS", "X PRESS FEEDER", "X PRESS FEEDERS S A PAK"),
    "Asia Trade": ("ASIA TRADE",),
    "Star Shipping": ("STAR SHIPPING",),
    "Universal Shipping": ("UNIVERSAL SHIPPING",),
    "Bulk Shipping Agencies": ("BULK SHIPPING AGENCIES",),
    "Bulk Shipping & Trading": ("BULK SHIPPING TRADING",),
    "Crystal Sea Services": ("CRYSTAL SEA SERVICES",),
    "Crystal Shipping": ("CRYSTAL SHIPPING",),
    "Pakistan National Shipping Corporation (PNSC)": ("P N S C", "PNSC", "PAKISTAN NATIONAL SHIP CORPT",
                                                      "PAKISTAN NATIONAL SHIPPING CORPORATION"),
    "Northstar": ("NORTHSTAR", "NORTH STAR"),
    "Legend Shipping": ("LEGEND SHIPPING",),
    "Sea Hawks": ("SEA HAWKS", "SEAHAWKS"),
    "Seahawks Asia Global": ("SEAHAWKS ASIA GLOBAL",),
    "Transtrade": ("TRANSTRADE", "TRANS TRADE"),
    "OOCL Pakistan": ("OOCL PAKISTAN",),
    "Sea Trade Shipping": ("SEA TRADE SHIPPING", "SEATRADE SHIPPING"),
    "Trans Maritime": ("TRANS MARITIME",),
    "Associated Liner": ("ASSOCIATED LINER",),
    "Waterlink Pakistan": ("WATERLINK PAK", "WATERLINK PAKISTAN"),
    "Sharaf Shipping": ("SHARAF SHIPPING",),
    "WMA Shipcare": ("WMA SHIPCARE",),
    "General Shipping": ("GENERAL SHIPPING",),
    "General Maritime": ("GENERAL MARITIME",),
    "Hussain Trading": ("HUSSAIN TRADING",),
    "Evergreen Shipping": ("EVERGREEN SHIPPING",),
    "Gulf Maritime": ("GULF MARITIME",),
    "Merchant Marine Services": ("MERCHANT MARINE SERVICES", "MERCHANT MARINE"),
    "Oceansea Shipping": ("OCEANSEA SHIPPING",),
    "International Shipping Ports": ("INTERNATIONAL SHIPPING PORTS",),
    "Gearbulk Shipping": ("GEARBULK SHIPPING",),
    "Pak Liner Agencies": ("PAK LINER AGENCIES",),
    "Poseidon Shipping": ("POSEIDON SHIPPING", "POSIDON", "POSEIDON"),
    "Maritime Agencies": ("MARITIME AGENCIES",),
    "Burjorjee Cowasjee": ("BURJORJEE COWASJEE",),
    "Cowasjee Sons": ("COWAJEE SONS", "COWASJEE SONS"),
    "Riazeda": ("RIAZEDA",),
    "Inchcape Shipping Services": ("INCH CAPE SHIPPING SERVICES", "INCHCAPE SHIPPING SERVICES"),
    "Swift Shipping": ("SWIFT SHIPPING",),
    "Novamarine": ("NOVAMARINE", "NOVA MARINE"),
    "Coastal Shipping": ("COASTAL SHIPPING",),
    "Mackinnon Mackenzie": ("MACKINNON MACKENZIE",),
    "Ocean Crest": ("OCEAN CREST",),
    "United Marine Agencies": ("UNITED MARINE", "UNITED MARINE AGENCIES"),
    "Crescent Ocean Marine": ("CRESENT OCEAN MARINE", "CRESCENT OCEAN MARINE"),
    "M.M. Services": ("M M SERVICES",),
    "GM Shipping Operations": ("GM SHIPPING OPER", "GM SHIPPING OPERATIONS"),
    "NYK Line Pakistan": ("NYK LINE PAKISTAN", "NYK LINE"),
    "Sirius Logistics": ("SIRIOUS LOGISTIC", "SIRIUS LOGISTICS"),
    "APL Pakistan": ("APL PAKISTAN",),
    "Port Qasim Authority": ("PQA",),
    "Facilities Shipping": ("FACILITIES SHIPPING",),
    "Forbes Shipping": ("FORBES SHIPPING COM", "FORBES SHIPPING"),
    "Diamond Shipping Services": ("DIAMOND SHIPPING", "DIAMOND SHIPPING SERVICES"),
    "Sinopak": ("SINOPAK",),
    "Seacon": ("SEACON",),
    "Noble Shipping": ("NOBLE SHIPPING",),
    "Project Shipping": ("PROJECT SHIPPING",),
    "Anchor Logistics": ("ANCHOR LOGISTICS",),
    "AP Line": ("AP LINE",),
}
_LEGAL = re.compile(r"\b(?:PVT|PRIVATE|LTD|LIMITED|CO|COMPANY|INC|SMC|S A)\b")


def _akey(text: str) -> str:
    return re.sub(r"\s+", " ", _LEGAL.sub(" ", _key(text))).strip()


_AGENT = {_akey(v): k for k, vs in AGENTS.items() for v in vs} | {_akey(k): k for k in AGENTS}


def agent(raw: str) -> str | None:
    """Raw agent text (possibly cut at 16 letters, possibly with '(Pvt) Ltd') -> one readable name. Pure."""
    k = _akey(raw)
    if not k:
        return None
    if k in _AGENT:
        return _AGENT[k]
    # cut short by the anchorage table (and maybe minus the letter that ran into the flag)
    hits = {v for a, v in _AGENT.items() if len(k) >= 9 and a.startswith(k)}
    if len(hits) == 1:
        return hits.pop()
    return tidy(re.sub(r"\(?\b(?:Pvt|PVT|pvt)\)?\.?\s*(?:Ltd|LTD|Limited)?\.?$", "", raw.strip()).strip(" ."))


# ---- countries (flags and ports) ---------------------------------------------------------------------------
COUNTRIES: dict[str, tuple[str, ...]] = {
    "Panama": ("PANAMA",), "Liberia": ("LIBERIA", "LIBERIAN", "MONOROVIA", "MONROVIA", "MANROVIA"),
    "Marshall Islands": ("M ISLAND", "MARSHALL ISLANDS", "MARSHAL ISLAND", "MAJURO", "MUJURA"),
    "Singapore": ("SINGAPORE", "SINGAPUR", "SINGHAPURE"), "Hong Kong": ("HONGKONG", "HONG KONG"),
    "Malta": ("MALTA",), "Bahamas": ("BAHAMAS",), "China": ("CHINA", "CHAINA", "PRC"),
    "Cyprus": ("CYPRUS",), "Kuwait": ("KUWAIT",), "Greece": ("GREECE", "GREEK", "HELLENIC"),
    "Pakistan": ("PAKISTAN",), "Bangladesh": ("BANGLADESH",), "Portugal": ("PORTUGAL",), "Comoros": ("COMOROS", "COMON ISLA", "CAMONIST"),
    "Palau": ("PALAU",), "Denmark": ("DENMARK", "DANISH"), "United Kingdom": ("BRITISH", "UK", "U K", "UNITED KINGDOM"),
    "South Korea": ("S KOREA", "KOREA", "SOUTH KOREA"), "North Korea": ("N KOREA",),
    "Philippines": ("PHILLIPPIN", "PHILIPPINES", "MANILA"), "Saudi Arabia": ("SAUDI ARAB", "SAUDI ARABIA", "S ARABIA"),
    "Barbados": ("BARBADOS",), "Norway": ("NORWAY",), "Antigua and Barbuda": ("ANTIGUA BR", "ANTIGOVA", "BARBUDA", "ANTIGUA"),
    "India": ("INDIA",), "Germany": ("GERMANY", "HAMBURG"), "Vietnam": ("VEITENAM", "VIETNAM", "VITNAM"),
    "United States": ("U S A", "USA", "UNITED STATES"), "Thailand": ("THAILAND",), "Tuvalu": ("TUVALU", "TAVALU"),
    "Cayman Islands": ("CAYMAN", "KAYMEN IS", "CAYMAN ISLANDS"), "Bermuda": ("BERMUDA",), "Italy": ("ITALY", "ITALT"),
    "Belize": ("BELIZE",), "Saint Kitts and Nevis": ("ST KITTS N", "ST KITTS"), "Turkey": ("TURKEY",),
    "Malaysia": ("MALAYSIA", "MALASIA"), "Cook Islands": ("C ISLAND", "COOK ISLAN", "COOK ISLANDS"),
    "Tanzania": ("TANZANIA",), "Saint Vincent and the Grenadines": ("ST VIN GRE", "ST VINCANT", "SAINT VINC", "ST VINCENT"),
    "Indonesia": ("INDONESIA", "INDONASIA", "INDONESHIA", "INDONASHIA", "INDONISA"), "Switzerland": ("SW LAND",),
    "Gabon": ("GABON",), "Togo": ("TOGO",), "Netherlands": ("NETHERLAND", "NATHERLAND", "NETHERLANDS"),
    "Qatar": ("QATAR", "QATTAR", "QATER"), "France": ("FRANCE", "FRENCH"), "Japan": ("JAPAN",),
    "Sri Lanka": ("SRILANKA", "SRI LANKA"), "Belgium": ("BELGIUM",), "Sierra Leone": ("SIEERA", "SERALEON", "SIERRA LEONE"),
    "Gambia": ("GAMBIA",), "Djibouti": ("DJIBOUTI",), "Sao Tome and Principe": ("SAO TOME", "ESAO TOME"),
    "San Marino": ("S MARINO", "SMARINO"), "Libya": ("LIBYA", "LIBYAN"), "Haiti": ("HAITI",), "Vanuatu": ("VANUATU",),
    "Egypt": ("EGYPT",), "Seychelles": ("SEYCHELLES",), "Spain": ("SPAIN",), "Jordan": ("JORDAN",),
    "Cameroon": ("CEMEROON", "CAMEROON"), "Croatia": ("CROTIAN", "CROATIA"), "Russia": ("RUSSIA", "RASIA"),
    "Algeria": ("ALGERIA",), "Dominica": ("DOMINICA",), "United Arab Emirates": ("U A E", "UAE"),
    "Isle of Man": ("ISLOF MAN", "ISLE OF MAN"), "Ethiopia": ("ETHOPIA", "ETHIOPIA"), "South Africa": ("S AFRICA", "SOUTH AFRICA"),
    "Guinea-Bissau": ("G BISSAU",), "Papua New Guinea": ("P N GUINEA",), "Brunei": ("B DARUSSAL",),
    "Gibraltar": ("GIBRALTER", "GIBRALTAR"), "Argentina": ("ARGENTINA", "ARGENTINE"), "Brazil": ("BRAZIL", "BARAZIL", "BRAZEEL", "BRAZILE"),
    "Lebanon": ("LEBANON",), "Nauru": ("NAURU",), "Bahrain": ("BAHRAIN", "BAHRAN"), "Kosovo": ("KOSOVO",),
    "Oman": ("OMAN",), "Iraq": ("IRAQ",), "Canada": ("CANADA",), "Morocco": ("MOROCCO", "MOROCO", "MORROCO"),
    "Australia": ("AUSTRALIA", "AUSTRELIA", "AUSTRLIA", "AUSTRALLIA"), "Romania": ("ROMANIA", "ROMANIYA"),
    "Taiwan": ("TAIWAN", "TIWAN"), "Mozambique": ("MOZAMBIQUE", "MUZABIQUE", "MOZAMBEQ", "MOZAMBIK", "MOZUMBEEK"),
    "Ukraine": ("UKRAIN", "UKRAINE", "UKRINE", "UK RAIN"), "Nigeria": ("NIGERIA",), "Angola": ("ANGOLA",), "Iran": ("IRAN",),
    "Sudan": ("SUDAN",), "Kenya": ("KENYA",), "Colombia": ("COLOMBIA",), "Mexico": ("MEXICO",), "Chile": ("CHILE",),
    "Peru": ("PERU",), "Latvia": ("LATVIA",), "Estonia": ("ESTONIA",), "Lithuania": ("LITHUANIA",), "Poland": ("POLAND",),
    "Bulgaria": ("BULGARIA",), "Georgia": ("GEORGIA",), "Kazakhstan": ("KAZAKHSTAN",), "Yemen": ("YEMEN",),
    "Sweden": ("SWEDEN",), "Finland": ("FINLAND",), "Ireland": ("IRELAND",), "Mongolia": ("MONGOLIA",),
}
# ports (as PQA spells them) -> country
PORTS: dict[str, tuple[str, ...]] = {
    "South Africa": ("RICHARDS BAY", "RICHARD BAY", "RICHARDBAY", "RICHARD BEY", "RICHERD BAY", "RICHARDS BEY",
                     "RICHARED BEY", "RICHERDS BAY", "RICHAREDS BAY", "RICHERDBAY", "RICHEARDS BEY", "RIICHARD BEY",
                     "DURBAN", "SALDANHA BAY"),
    "United Arab Emirates": ("FUJAYRAH", "FUJAIRAH", "FUJERAH", "FUJAYRA", "FUJIARAH", "FUJAYIRAH", "FUJERA", "JEBEL ALI",
                             "JABAL ALI", "JABEL ALI", "J ALI", "RUWAIS", "RUWAYS", "KHOR FAKKAN", "HAMRIYAH", "HAMARYA",
                             "HOJORAH", "DAS ISLAND", "DUBAI", "SHARJAH", "ABU DHABI"),
    "Saudi Arabia": ("AL JUBAIL", "ALJUBAIL", "JUBAIL", "SHOIBAH", "SHOAIBA", "YANBU", "JEDDAH", "DAMMAM", "RAS TANURA"),
    "Oman": ("SOHAR", "SALALAH", "MUSCAT", "DUQM", "QALHAT", "QASAB", "KASAB"),
    "Qatar": ("RAS LAFFAN", "RASLAFFAN", "RASLAFAN", "MESAIEED"),
    "Kuwait": ("MINA ABDULLAH", "MEENAAL AHMED", "MEENA AL AHMED", "MINA AL AHMADI", "SHUAIBA", "SHUWAIKH"),
    "Iraq": ("BASRA", "BASRA IRAQ", "AL ZUBAIR", "KHOR AL ZUBAIR", "UMM QASR"),
    "Bahrain": ("SITRA", "SITRAH", "CITRA BAQRAN"),
    "Indonesia": ("DUMAI", "DUMASI", "TARAHAN", "TANJUNG BARA", "TABONEO", "BELAWAN", "TONA TAKRA", "KUALA TANJUNG"),
    "Malaysia": ("PORT KLANG", "PASIR GUDANG", "LAHAD DATU", "KUANTAN"),
    "China": ("SHANGHAI", "SHANGHI", "SHENGHAI", "SHANGAI", "DALIAN", "TIANJIN", "QINGDAO", "NINGBO", "XINGANG"),
    "Japan": ("KASHIMA",), "Mozambique": ("MAPUTO", "MATOLA", "NACALA"), "Brazil": ("SANTOS", "PARANAGUA", "PARANGOA"),
    "United States": ("NEW ORLEANS", "ORLEANS", "HOUSTON", "BATON ROUGE", "PORT LAND", "PORTLAND", "CORPUS CHRISTI"),
    "Canada": ("VANCOUVER", "VANCOVER", "VANCOUVRE", "VANVOUVRE", "VANCUVER"),
    "Netherlands": ("AMSTERDAM", "AMSTERSAM", "ROTTERDAM", "ROTTER DAM"), "Russia": ("TAMAN", "TANMA", "TMAN", "KAVKAZ", "NOVOROSSIYSK"),
    "Australia": ("GEELONG", "NEWCASTLE", "PORT KEMBLA", "GLADSTONE"), "Sri Lanka": ("COLUMBO", "COLOMBO"),
    "India": ("KANDLA", "MUMBAI", "MUNDRA", "CHENNAI", "VIZAG"),
    "Morocco": ("JORF LASFAR",), "Egypt": ("SUEZ",),
}
HOME = ("PQA", "P Q A", "PORT QASIM", "KARACHI", "KPT", "LOCAL", "BIN QASIM", "DIFF", "DIFFRENT", "DIFFERENT",
        "VARIOUS")
_HOME = {_key(h) for h in HOME}
_COUNTRY = ({_key(v): k for k, vs in COUNTRIES.items() for v in vs} | {_key(k): k for k in COUNTRIES}
            | {_key(v): k for k, vs in PORTS.items() for v in vs})


def country(raw: str) -> str | None:
    """A flag or a port as printed -> its country; None for Pakistan's own ports, 'DIFF(ERENT)' and
    anything not recognised. A letter run in from the agent column ('EPANAMA', 'SLIBERIA') is dropped. Pure."""
    k = _key(raw)
    if not k or re.fullmatch(r"[\d ]+", k) or k in _HOME:
        return None
    if k in _COUNTRY:
        return _COUNTRY[k]
    for cut in (1, 2):  # the agent's cut-off letters ran into the cell
        if len(k) > cut + 3 and k[cut:].lstrip() in _COUNTRY:
            return _COUNTRY[k[cut:].lstrip()]
    return None
