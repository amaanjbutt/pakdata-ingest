"""One registry of Pakistan's river gauging sites — dams, barrages, headworks and river gauges —
shared by every water source.

WAPDA's river-flow workbook ("INDUS AT TARBELA", "KABUL INFLOW AT NOWSHERA", "ESCAPAGE BELOW
KOTRI") and the PMD Flood Forecasting Division's daily bulletins ("Punjnad", "G.S. Wala",
"Kalabagh") each name the same structures their own way. `canon_site()` maps any spelling to one
readable site name plus its river:

    site     the structure or gauge, as people know it ("Tarbela", "Panjnad", "Ganda Singh Wala")
    river    the river it sits on (Indus, Kabul, Jhelum, Chenab, Ravi, Sutlej)

Same pattern as `ingestion/plants_pk.py` for power plants. Add every new publisher spelling as
an alias; an unknown name raises in the parsers rather than becoming a new site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

RIVERS = ("Indus", "Kabul", "Jhelum", "Chenab", "Ravi", "Sutlej")


@dataclass(frozen=True)
class Site:
    name: str
    river: str
    kind: str                      # dam, barrage, headworks, gauge
    province: str
    aliases: tuple[str, ...] = field(default_factory=tuple)


def S(name: str, river: str, kind: str, province: str, *aliases: str) -> Site:
    return Site(name, river, kind, province, tuple(aliases))


# Upstream to downstream, river by river (the bulletins' own order).
SITES: list[Site] = [
    S("Tarbela", "Indus", "dam", "Khyber Pakhtunkhwa", "Indus at Tarbela"),
    S("Kalabagh", "Indus", "barrage", "Punjab", "Jinnah", "Jinnah Barrage"),
    S("Chashma", "Indus", "barrage", "Punjab"),
    S("Taunsa", "Indus", "barrage", "Punjab"),
    S("Guddu", "Indus", "barrage", "Sindh"),
    S("Sukkur", "Indus", "barrage", "Sindh"),
    S("Kotri", "Indus", "barrage", "Sindh", "Below Kotri", "Escapage below Kotri"),
    S("Nowshera", "Kabul", "gauge", "Khyber Pakhtunkhwa", "Kabul at Nowshera", "Kabul inflow at Nowshera"),
    S("Mangla", "Jhelum", "dam", "Azad Kashmir", "Jhelum at Mangla", "Jehlum at Mangla"),
    S("Rasul", "Jhelum", "barrage", "Punjab"),
    S("Marala", "Chenab", "headworks", "Punjab", "Chenab at Marala", "Chenab inflow at Marala"),
    S("Khanki", "Chenab", "headworks", "Punjab"),
    S("Qadirabad", "Chenab", "barrage", "Punjab"),
    S("Chiniot Bridge", "Chenab", "gauge", "Punjab", "Chiniot"),
    S("Trimmu", "Chenab", "barrage", "Punjab"),
    S("Panjnad", "Chenab", "headworks", "Punjab", "Punjnad"),
    S("Jassar", "Ravi", "gauge", "Punjab"),
    S("Shahdara", "Ravi", "gauge", "Punjab"),
    S("Balloki", "Ravi", "headworks", "Punjab", "Baloki"),
    S("Sidhnai", "Ravi", "barrage", "Punjab"),
    S("Ganda Singh Wala", "Sutlej", "gauge", "Punjab", "G.S. Wala", "G.S.Wala", "GS Wala", "G. S. Wala"),
    S("Sulemanki", "Sutlej", "headworks", "Punjab"),
    S("Islam", "Sutlej", "headworks", "Punjab"),
]

# Weather stations in Tarbela's catchment whose daily temperatures WAPDA tracks (snow and glacier
# melt drive the Indus inflow), with the district each sits in.
CATCHMENT_STATIONS: dict[str, str] = {
    "Skardu": "Skardu", "Besham": "Shangla", "Tarbela": "Haripur", "Phulra": "Mansehra",
    "Daggar": "Buner", "Shinkiari": "Mansehra", "Oghi": "Mansehra",
}


def _key(s: str) -> str:
    return re.sub(r"[^a-z]+", " ", s.lower()).strip()


_BY_KEY: dict[str, Site] = {}
for _s in SITES:
    for _n in (_s.name, *_s.aliases):
        _BY_KEY[_key(_n)] = _s


def canon_site(raw: str) -> Site | None:
    """'Punjnad' / 'PANJNAD' -> Site('Panjnad', 'Chenab', ...); None when unknown. Pure."""
    return _BY_KEY.get(_key(raw))


def canon_station(raw: str) -> str | None:
    """'SHINKIARI ' -> 'Shinkiari' (a catchment weather station); None when unknown. Pure."""
    k = _key(raw)
    return next((s for s in CATCHMENT_STATIONS if _key(s) == k), None)
