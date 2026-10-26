"""Readable mandi (AMIS) market and commodity names, and one crop identity across provinces.

- `market_display(name, slug)`: "RahimYarKhan" -> "Rahim Yar Khan", "Karachi Market" -> "Karachi".
- `market_region(name, province)`: where the market is. Punjab's list also reports a few markets
  outside Punjab (Karachi, Hyderabad, Quetta, Gwadar); `province` stays the list that reports it.
- `commodity_display(name)`: "Bitter Gourd (کریلا)" -> ("Bitter Gourd", "کریلا").
- `crop_key(slug, province)`: Sindh's graded varieties mapped onto the Punjab commodity of the same
  crop ("sindh-onion_grade_1" -> "onion"), so a commodity page can show both provinces. Only
  mapped where it is clearly the same crop and both are priced by weight (or both per dozen).

Pure; the frontend mirrors none of this any more — it reads `display_name` from the API.
"""
from __future__ import annotations

import re

MARKET_NAMES = {
    "abdulhakim": "Abdul Hakim", "ahmadpureast": "Ahmadpur East", "alipurchatta": "Alipur Chatha",
    "arifwala": "Arifwala", "bahawalnagar": "Bahawalnagar", "bahawalpur": "Bahawalpur", "bhakhar": "Bhakkar",
    "chackjhumra": "Chak Jhumra", "chistian": "Chishtian", "chuasaidanshah": "Choa Saidan Shah",
    "dgkhan": "Dera Ghazi Khan", "dunyapur": "Dunyapur", "fortabas": "Fort Abbas", "ghakhar": "Ghakhar Mandi",
    "gujarkhan": "Gujar Khan", "haroonabad": "Haroonabad", "hasalpur": "Hasilpur", "hasanabdal": "Hasan Abdal",
    "havelilakha": "Haveli Lakha", "hujrashahmuqeem": "Hujra Shah Muqeem", "jalalpurjattan": "Jalalpur Jattan",
    "jalalpurpirwala": "Jalalpur Pirwala", "jampur": "Jampur", "kabirwala": "Kabirwala", "kachakhu": "Kacha Khuh",
    "kahrorpacca": "Kahror Pacca", "katchalahore": "Kacha (Lahore)", "khairpurtamewali": "Khairpur Tamewali",
    "khankahdogran": "Khanqah Dogran", "kotadu": "Kot Addu", "kotchutta": "Kot Chutta", "kotmoman": "Kot Momin",
    "kotradhakishan": "Kot Radha Kishan", "lahore_singhpura": "Lahore (Singhpura)", "lalamusa": "Lala Musa",
    "liaqatpur": "Liaquatpur", "mamunkanjan": "Mamu Kanjan", "mandibahaudin": "Mandi Bahauddin",
    "mianchannu": "Mian Channu", "mithatiwana": "Mitha Tiwana", "multanroadlahore": "Multan Road (Lahore)",
    "muzafarghar": "Muzaffargarh", "nankana": "Nankana Sahib", "narangmandi": "Narang Mandi",
    "noshehrawirkan": "Nowshera Virkan", "pakpattan": "Pakpattan", "pasroor": "Pasrur", "phoolnagar": "Phool Nagar",
    "pindibhattian": "Pindi Bhattian", "pirmahal": "Pir Mahal", "qadirpurrawan": "Qadirpur Rawan",
    "qiladedarsingh": "Qila Didar Singh", "rahimyarkhan": "Rahim Yar Khan", "rajanpur": "Rajanpur",
    "renalakhurd": "Renala Khurd", "sadiqabad": "Sadiqabad", "sanglahill": "Sangla Hill", "shahjewana": "Shah Jewana",
    "shahrsultan": "Shehr Sultan", "shujaabad": "Shujabad", "sraialamgir": "Sarai Alamgir", "summandri": "Samundri",
    "talagang": "Talagang", "taunsasharif": "Taunsa Sharif", "ttsingh": "Toba Tek Singh", "warberten": "Warburton",
    "badomalhi": "Badomalhi", "kanganpur": "Kanganpur", "pinanwal": "Pinanwal",
}

# Punjab's list reports these markets outside Punjab
OUTSIDE_PUNJAB = {"karachi": "Sindh", "hyderabad": "Sindh", "quetta": "Balochistan", "gwadar": "Balochistan"}

SINDH_DISTRICTS = {"Karachi (Division)": "Karachi", "Kashmore @ Kandhkot": "Kashmore",
                   "Kambar @ Shahdadkot": "Kambar Shahdad Kot"}


def market_display(name: str, slug: str = "") -> str:
    key = re.sub(r"^sindh[-_]", "", (slug or "")).lower()
    if key in MARKET_NAMES:
        return MARKET_NAMES[key]
    n = re.sub(r"\s+Market$", "", name.strip())
    if n.isupper():
        return n.title()
    # AMIS run-together camel case: "BahawalNagar" -> "Bahawalnagar", "RenalaKhurd" -> "Renala Khurd"
    words = re.sub(r"\(", " (", n).split()
    out = []
    for w in words:
        for part in re.split(r"(?<=[a-z])(?=[A-Z])", w):
            if out and part in ("Pur", "Wala", "Abad", "Nagar", "Ghar", "Pattan"):
                out[-1] += part.lower()
            else:
                out.append(part[:1].upper() + part[1:])
    return " ".join(out)


def market_region(name: str, province: str) -> str:
    if province == "Punjab":
        return OUTSIDE_PUNJAB.get(name.strip().lower(), "Punjab")
    return province


def market_district(display: str, region: str, raw_district: str | None = None) -> str | None:
    from ingestion.geo_pk import canon_district

    if raw_district:
        d = SINDH_DISTRICTS.get(raw_district, raw_district)
        hit = canon_district(d, region)
        return hit[1] if hit else d
    hit = canon_district(display, region if region in ("Punjab", "Sindh", "Balochistan") else None)
    return hit[1] if hit else None


_URDU = re.compile(r"[؀-ۿ]")

# AMIS's own misspellings, fixed in the display name and the slug (the /mandi/{crop} URL); the raw
# text stays in `name`. Old slugs keep resolving through SLUG_RENAMES (API) and the site's redirect.
SPELLING = [(r"\bCocunut\b", "Coconut"), (r"\bSuger\b", "Sugar"), (r"\bDesahri\b", "Dusehri"),
            (r"\bChounsa\b", "Chaunsa"), (r"\bAnwer Ratol\b|\bAnwertol\b", "Anwar Ratol"), (r"\bJaman\b", "Jamun"),
            (r"\bPees\b", "Peas")]
SLUG_RENAMES = {
    "cocunut": "coconut", "suger_beet": "sugar_beet", "mango_desahri": "mango_dusehri",
    "mango_chounsa": "mango_chaunsa", "mango_anwer_ratol": "mango_anwar_ratol", "jaman": "jamun",
    "sindh_mango_anwertol": "sindh_mango_anwar_ratol", "sindh_pees_classic": "sindh_peas_classic",
    "sindh_pees_grade_1": "sindh_peas_grade_1", "sindh_pees_grade_2": "sindh_peas_grade_2",
    "sindh_pees_malir": "sindh_peas_malir", "sindh_pees_punjab": "sindh_peas_punjab",
}


def fix_spelling(text: str) -> str:
    for pat, rep in SPELLING:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


def commodity_display(name: str) -> tuple[str, str | None]:
    """(English name, Urdu name or None). "Banana(DOZEN)" -> "Banana (per dozen)"."""
    urdu = None
    parts = []
    for m in re.finditer(r"\(([^)]*)\)|[^()]+", name):
        inner = m.group(1)
        if inner is not None:
            if _URDU.search(inner):
                urdu = inner.strip()
            else:
                parts.append(f" ({inner.strip()})")
        else:
            seg = m.group(0)
            tail = re.search(r"\s*([؀-ۿ][؀-ۿ\s]*)$", seg)
            if tail:
                urdu = urdu or tail.group(1).strip()
                seg = seg[: tail.start()]
            parts.append(seg)
    s = re.sub(r"\s+", " ", "".join(parts)).strip()
    s = re.sub(r"\(\s*DOZEN\s*\)", "(per dozen)", s, flags=re.I)
    s = re.sub(r"\((\d+)\s*Pcs\)", r"(\1 pieces)", s, flags=re.I)
    s = fix_spelling(s)
    return (s[:1].upper() + s[1:]), urdu


# Sindh commodity slug (without "sindh-") -> Punjab commodity slug of the same crop
SINDH_CROPS = {
    "onion_grade_1": "onion", "onion_punjab": "onion", "onion_quetta": "onion", "onion_small": "onion",
    "tomato": "tomato", "tomato_grade_1": "tomato", "tomato_grade_2": "tomato",
    "potato_grade_1": "potato_fresh", "potato_grade_2": "potato_fresh", "new_potato": "potato_fresh",
    "red_potato_grade_1": "potato_fresh", "red_potato_grade_2": "potato_fresh",
    "garlic_grade_1": "garlic_local", "garlic_grade_2": "garlic_local", "sindhi_garlic": "garlic_local",
    "garlic_china": "garlic_china",
    "ginger_china_grade_1": "ginger_china", "ginger_china_grade_2": "ginger_china",
    "ginger_without_wash_bangkok": "ginger_thai",
    "chilli": "green_chilli", "chilli_moro": "green_chilli", "chilli_shehzady": "green_chilli",
    "chili_mirch_gajju": "green_chilli", "chili_mirch_talhari": "green_chilli",
    "lemon_grade_1": "lemon_desi", "lemon_grade_2": "lemon_desi", "lemon_china": "lemon_china",
    "banana_grade_1_per_dozen": "banana_dozen", "banana_grade_2_per_dozen": "banana_dozen",
    "apple_aamri_grade_1": "apple_ammre", "apple_aamri_grade_2": "apple_ammre",
    "apple_golden_grade_1": "apple_golden", "apple_golden_grade_2": "apple_golden",
    "apple_gaja": "apple_gatcha",
    "bitter_gourd_grade_1": "bitter_gourd", "bitter_gourd_grade_2": "bitter_gourd",
    "long_bottle_gourd": "bottle_gourd", "round_bottle_gourd": "bottle_gourd",
    "long_brinjal": "brinjal", "round_brinjal": "brinjal",
    "cabbage_grade_1": "cabbage", "cabbage_grade_2": "cabbage",
    "capsicum_grade_1": "capsicum", "capsicum_grade_2": "capsicum",
    "carrot_red": "carrot", "lahori_gajar": "carrot", "carrot_china": "carrot_china",
    "cauliflower_grade_1": "cauliflower", "cauliflower_grade_2": "cauliflower",
    "cucumber_grade_1": "cucumber_kheera", "cucumber_grade_2": "cucumber_kheera",
    "dhania_gaddi": "coriander", "methi": "fenugreek", "methi_choti": "fenugreek", "podina_gaddi": "mint",
    "green_chickpeas": "green_chickpeas", "green_onion": "green_onion",
    "lady_finger_grade_1": "lady_finger_okra", "lady_finger_grade_2": "lady_finger_okra",
    "moongray": "mongray", "sarson_saag": "mustard_greens",
    "peas_classic": "peas", "peas_grade_1": "peas", "peas_grade_2": "peas", "peas_malir": "peas", "peas_punjab": "peas",
    "radish": "radish", "mooli_grade_2": "radish",
    "spinach": "spinach", "sindhi_palak": "spinach", "palak_walaiti": "spinach", "english_palak": "spinach",
    "tinda_desi": "tinda_desi", "tinda_grade_2": "tinda_desi", "sindhi_tinda": "tinda_desi",
    "shaljum_with_leaves": "turnip", "shaljum_without_leaves": "turnip",
    "chukandar_with_leaves": "sugar_beet", "chukandar_without_leaves": "sugar_beet",
    "arvi": "cocoyam", "turai_grade_1": "zucchini", "turai_grade_2": "zucchini", "sindhi_turai": "zucchini",
    "english_turai": "zucchini",
    "date_aseel_grade_2": "dates_aseel", "date_irani_grade_1": "dates_irani", "date_irani_grade_2": "dates_irani",
    "guava_hyderabad": "guava", "guava_kusoor": "guava", "guava_larkana": "guava",
    "jamun": "jamun", "bair": "jujube",
    "mango_anwar_ratol": "mango_anwar_ratol", "mango_chaunsa": "mango_chaunsa", "mango_desi": "mango_desi",
    "melon_grade_1": "melon", "melon_grade_2": "melon", "papaya_grade_1": "papaya", "papaya_grade_2": "papaya",
    "peach_grade_1": "peach", "peach_grade_2": "peach", "pear": "pear", "plum": "plum",
    "persimmon_amlok_grade_1": "persimmon", "persimmon_amlok_grade_2": "persimmon",
    "pomegranate_kandhari_grade_1": "pomegranate_kandhari", "pomegranate_kandhari_grade_2": "pomegranate_kandhari",
    "sugar_cane": "sugarcane",
    "watermelon_long": "watermelon", "watermelon_round": "watermelon", "striped_watermelon": "watermelon",
}


def crop_key(slug: str, province: str) -> str:
    if province == "Sindh":
        bare = re.sub(r"^sindh[-_]", "", slug)
        return SINDH_CROPS.get(bare, slug)
    return slug


# Rs per kg from any of the AMIS units (dozen-priced items stay per dozen)
def per_kg(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit == "pkr_per_100kg":
        return value / 100
    if unit == "pkr_per_kg":
        return value
    return None


def apply_names(conn) -> dict:
    """(Re)compute display_name / region / district / urdu_name / crop_key for every market and
    commodity, and put Sindh slugs in the job's form (`sindh_<name>`). Idempotent; ~450 rows."""
    from ingestion.jobs.amis_prices import slug as _slug

    with conn.cursor() as cur:
        cur.execute("SELECT market_id, name, slug, province, district FROM agri_markets")
        markets = cur.fetchall()
        cur.execute("SELECT commodity_id, name, slug, province FROM agri_commodities")
        comms = cur.fetchall()
        m_rows = []
        for mid, name, slug, prov, dist in markets:
            if prov == "Sindh":
                slug = "sindh_" + _slug(re.sub(r"\s+Market$", "", name))
            disp = market_display(name, slug)
            region = market_region(name, prov)
            m_rows.append((disp, region, market_district(disp, region, dist), slug, mid))
        cur.executemany("""UPDATE agri_markets SET display_name = %s, region = %s,
                               district = COALESCE(%s, district), slug = %s WHERE market_id = %s""", m_rows)
        c_rows = []
        for cid, name, slug, prov in comms:
            if prov == "Sindh":
                slug = "sindh_" + _slug(name)
            slug = SLUG_RENAMES.get(slug, slug)
            disp, urdu = commodity_display(name)
            c_rows.append((disp, urdu, crop_key(slug, prov), slug, cid))
        cur.executemany("""UPDATE agri_commodities SET display_name = %s, urdu_name = %s, crop_key = %s,
                               slug = %s WHERE commodity_id = %s""", c_rows)
    return {"markets": len(m_rows), "commodities": len(c_rows)}
