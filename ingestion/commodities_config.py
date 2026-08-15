"""Loader for the SPI commodities mapping (ingestion/commodities_config.json)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "commodities_config.json")


@dataclass(frozen=True)
class CommodityItem:
    id: str
    name: str
    unit: str
    aliases: tuple[str, ...]
    min_value: float | None
    max_value: float | None
    max_step: float | None


def _load() -> dict:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_items() -> list[CommodityItem]:
    cfg = _load()
    return [
        CommodityItem(
            id=i["id"],
            name=i["name"],
            unit=i.get("unit", "pkr"),
            aliases=tuple(a.lower() for a in i.get("aliases", [])),
            min_value=i.get("min_value"),
            max_value=i.get("max_value"),
            max_step=i.get("max_step"),
        )
        for i in cfg["items"]
    ]


def load_cities() -> dict[str, str]:
    """Lowercase city label -> canonical city key."""
    return {k.lower(): v for k, v in _load()["cities"].items()}
