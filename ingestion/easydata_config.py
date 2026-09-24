"""Loader for the EasyData series mapping (ingestion/easydata_series.json)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "easydata_series.json")


@dataclass(frozen=True)
class EasyDataSeries:
    easydata_key: str
    id: str
    module: str
    name: str
    description: str
    unit: str
    frequency: str
    source: str
    tier: str
    min_value: float | None
    max_value: float | None
    max_step: float | None
    backfill_start: str
    easydata_dataset_code: str | None = None
    easydata_last_refresh: str | None = None


def load_series() -> list[EasyDataSeries]:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    out: list[EasyDataSeries] = []
    for s in cfg["series"]:
        out.append(
            EasyDataSeries(
                easydata_key=s["easydata_key"],
                id=s["id"],
                module=s["module"],
                name=s["name"],
                description=s.get("description", ""),
                unit=s.get("unit", ""),
                frequency=s["frequency"],
                source=s.get("source", "SBP"),
                tier=s.get("tier", "basic"),
                min_value=s.get("min_value"),
                max_value=s.get("max_value"),
                max_step=s.get("max_step"),
                backfill_start=s.get("backfill_start", "2000-01-01"),
                easydata_dataset_code=s.get("easydata_dataset_code"),
                easydata_last_refresh=s.get("easydata_last_refresh"),
            )
        )
    return out


def by_easydata_key() -> dict[str, EasyDataSeries]:
    return {s.easydata_key: s for s in load_series()}
