"""Pakistan's provinces and districts — one canonical spelling for every regional job.

Sources spell districts every which way ("Bahawalnager", "D.G.khan", "Jaccobabad", "T.T.Singh",
"Muzaffar Garh"). Regional series key places by `dims.province` / `dims.district`, so every job
maps its labels through `canon_district` onto the names the 2023 census uses (`DISTRICTS_2023`),
plus the names of districts that existed at earlier censuses and were later split or renamed
(`HISTORICAL`): those stay distinct places, they are not folded into their successors.
"""
from __future__ import annotations

import difflib
import re

PROVINCES = ("Punjab", "Sindh", "Khyber Pakhtunkhwa", "Balochistan", "Islamabad")
_PROVINCE_ALIASES = {
    "punjab": "Punjab", "sindh": "Sindh", "sind": "Sindh", "kp": "Khyber Pakhtunkhwa", "kpk": "Khyber Pakhtunkhwa",
    "khyber pakhtunkhwa": "Khyber Pakhtunkhwa", "khyber pakhtoonkhwa": "Khyber Pakhtunkhwa", "nwfp": "Khyber Pakhtunkhwa",
    "k.p.k": "Khyber Pakhtunkhwa", "k p": "Khyber Pakhtunkhwa", "balochistan": "Balochistan",
    "baluchistan": "Balochistan", "islamabad": "Islamabad", "ict": "Islamabad", "islamabad capital territory": "Islamabad",
}

DISTRICTS_2023 = {
    "Punjab": ["Attock", "Bahawalnagar", "Bahawalpur", "Bhakkar", "Chakwal", "Chiniot", "Dera Ghazi Khan", "Faisalabad",
               "Gujranwala", "Gujrat", "Hafizabad", "Jhang", "Jhelum", "Kasur", "Khanewal", "Khushab", "Lahore", "Layyah",
               "Lodhran", "Mandi Bahauddin", "Mianwali", "Multan", "Muzaffargarh", "Nankana Sahib", "Narowal", "Okara",
               "Pakpattan", "Rahim Yar Khan", "Rajanpur", "Rawalpindi", "Sahiwal", "Sargodha", "Sheikhupura", "Sialkot",
               "Toba Tek Singh", "Vehari"],
    "Sindh": ["Badin", "Dadu", "Ghotki", "Hyderabad", "Jacobabad", "Jamshoro", "Kambar Shahdad Kot", "Karachi Central",
              "Karachi East", "Karachi South", "Karachi West", "Kashmore", "Keamari", "Khairpur", "Korangi", "Larkana",
              "Malir", "Matiari", "Mirpur Khas", "Naushahro Feroze", "Sanghar", "Shaheed Benazirabad", "Shikarpur",
              "Sujawal", "Sukkur", "Tando Allahyar", "Tando Muhammad Khan", "Tharparkar", "Thatta", "Umer Kot"],
    "Khyber Pakhtunkhwa": ["Abbottabad", "Bajaur", "Bannu", "Batagram", "Buner", "Charsadda", "Dera Ismail Khan", "Hangu",
                           "Haripur", "Karak", "Khyber", "Kohat", "Kolai Palas Kohistan", "Kurram", "Lakki Marwat",
                           "Lower Chitral", "Lower Dir", "Lower Kohistan", "Malakand", "Mansehra", "Mardan", "Mohmand",
                           "North Waziristan", "Nowshera", "Orakzai", "Peshawar", "Shangla", "South Waziristan", "Swabi",
                           "Swat", "Tank", "Torghar", "Upper Chitral", "Upper Dir", "Upper Kohistan"],
    "Balochistan": ["Awaran", "Barkhan", "Chagai", "Chaman", "Dera Bugti", "Duki", "Gwadar", "Harnai", "Jaffarabad",
                    "Jhal Magsi", "Kachhi", "Kalat", "Kech", "Kharan", "Khuzdar", "Killa Abdullah", "Killa Saifullah",
                    "Kohlu", "Lasbela", "Loralai", "Mastung", "Musakhel", "Nasirabad", "Nushki", "Panjgur", "Pishin",
                    "Quetta", "Sherani", "Sibi", "Sohbatpur", "Surab", "Washuk", "Zhob", "Ziarat"],
    "Islamabad": ["Islamabad"],
}
# Districts of earlier censuses/surveys that were later split or merged — kept as their own places.
HISTORICAL = {
    "Sindh": ["Karachi"],
    "Khyber Pakhtunkhwa": ["Chitral", "Kohistan", "F.R. Bannu", "F.R. Dera Ismail Khan", "F.R. Kohat",
                           "F.R. Lakki Marwat", "F.R. Peshawar", "F.R. Tank"],
}
_ALIASES = {
    "bahawalnager": "Bahawalnagar", "bhawal nagar": "Bahawalnagar", "bhakhar": "Bhakkar", "d g khan": "Dera Ghazi Khan",
    "dg khan": "Dera Ghazi Khan", "d i khan": "Dera Ismail Khan", "di khan": "Dera Ismail Khan",
    "dera ismial khan": "Dera Ismail Khan", "jehlum": "Jhelum", "jaccobabad": "Jacobabad", "muzaffar garh": "Muzaffargarh",
    "mandi bahuddin": "Mandi Bahauddin", "m b din": "Mandi Bahauddin", "t t singh": "Toba Tek Singh",
    "tt singh": "Toba Tek Singh", "pakpatten": "Pakpattan", "r y khan": "Rahim Yar Khan", "ry khan": "Rahim Yar Khan",
    "raim yar khan": "Rahim Yar Khan", "shaiwal": "Sahiwal", "nawabshah": "Shaheed Benazirabad",
    "shaheed benazir abad": "Shaheed Benazirabad", "s benazirabad": "Shaheed Benazirabad", "s b abad": "Shaheed Benazirabad",
    "mirpurkhas": "Mirpur Khas", "umerkot": "Umer Kot", "umarkot": "Umer Kot", "umar kot": "Umer Kot",
    "kamber shahdadkot": "Kambar Shahdad Kot", "qambar shahdadkot": "Kambar Shahdad Kot",
    "kambar shahdadkot": "Kambar Shahdad Kot", "naushero feroze": "Naushahro Feroze", "n feroze": "Naushahro Feroze",
    "naushahro feroz": "Naushahro Feroze", "tando allah yar": "Tando Allahyar", "tando ahyar": "Tando Allahyar",
    "t m khan": "Tando Muhammad Khan", "tando m khan": "Tando Muhammad Khan", "kemari": "Keamari",
    "karachi keamari": "Keamari", "shikapur": "Shikarpur", "kohlui": "Kohlu", "bolan": "Kachhi", "kachi": "Kachhi",
    "kech turbat": "Kech", "turbat": "Kech", "lasbella": "Lasbela", "killa abdulla": "Killa Abdullah",
    "qila abdullah": "Killa Abdullah", "qila saifullah": "Killa Saifullah", "noshki": "Nushki", "naseerabad": "Nasirabad",
    "jafarabad": "Jaffarabad", "jhal magsi": "Jhal Magsi", "dera bugti": "Dera Bugti", "musa khel": "Musakhel",
    "peshaware": "Peshawar", "malakand protected area": "Malakand", "batgram": "Batagram", "abbotabad": "Abbottabad",
    "tor ghar": "Torghar", "lakki": "Lakki Marwat", "dir lower": "Lower Dir", "dir upper": "Upper Dir",
    "chitral lower": "Lower Chitral", "chitral upper": "Upper Chitral", "ict": "Islamabad",
    "karachi malir": "Malir", "karachi korangi": "Korangi", "karachi central": "Karachi Central",
    "nowshero feroze": "Naushahro Feroze", "shahdadkot": "Kambar Shahdad Kot", "bonair": "Buner", "tor garh": "Torghar",
    "hernai": "Harnai", "lasbilla": "Lasbela", "pashin": "Pishin", "shaheed sikandar abad": "Surab",
    "shaheed sikandarabad": "Surab", "sohbat pur": "Sohbatpur", "kolai pallas": "Kolai Palas Kohistan",
    "kolai palas": "Kolai Palas Kohistan", "s waziristan": "South Waziristan", "n waziristan": "North Waziristan",
}


def _key(s: str) -> str:
    s = re.sub(r"\b(district|city|zilla|agency)\b", " ", s.lower())
    return re.sub(r"[^a-z]+", " ", s).strip()


def canon_province(name: str) -> str | None:
    return _PROVINCE_ALIASES.get(_key(name)) or _PROVINCE_ALIASES.get(re.sub(r"[^a-z]", "", name.lower()))


_BY_KEY: dict[str, tuple[str, str]] = {}
for _p, _ds in list(DISTRICTS_2023.items()) + list(HISTORICAL.items()):
    for _d in _ds:
        _BY_KEY[_key(_d)] = (_p, _d)
        _BY_KEY[_key(_d).replace(" ", "")] = (_p, _d)
_PROV_OF = {d: p for k, (p, d) in _BY_KEY.items()}


def canon_district(name: str, province: str | None = None, cutoff: float = 0.86) -> tuple[str, str] | None:
    """(province, canonical district) for a source label, or None when nothing matches closely. Pure.

    Exact/alias match first, then a close fuzzy match within the province (if known)."""
    k = _key(name)
    if not k:
        return None
    alias = _ALIASES.get(k) or _ALIASES.get(k.replace(" ", ""))
    if alias:
        return _PROV_OF[alias], alias
    hit = _BY_KEY.get(k) or _BY_KEY.get(k.replace(" ", ""))
    if hit and (province is None or hit[0] == province):
        return hit
    pool = {kk: v for kk, v in _BY_KEY.items() if province is None or v[0] == province}
    m = difflib.get_close_matches(k, list(pool), n=1, cutoff=cutoff)
    return pool[m[0]] if m else None
