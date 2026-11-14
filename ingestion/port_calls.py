"""The ports vessel table (`port_vessel_calls`): one row per ship × commodity × direction in each daily
statement of a port (Port Qasim's "Daily Shipping and Cargo Statement", Karachi Port's daily tonnage
and container-handling reports), and Karachi's ship calls (section 'calls', dated by arrival, from its
Vessel History — kpt_vessel_history).

Each statement date is its own snapshot: a ship at a berth for five days has five rows, one per
statement, so a manifest or balance the port restates shows up from one day to the next — that day-by-day
record is this source's vintage history (ports publish no revisions otherwise). A statement re-read
replaces that date's rows (rows it no longer lists are removed), so re-running a day is idempotent.

Raw publisher text stays in the `*_raw` columns; readable names come from `ingestion/ports_pk.py`.
Jobs mix these rows with ordinary `Record`s (the derived daily series): `split()` separates them,
`PortCallsMixin` validates and upserts both.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class VesselRow:
    """One line of a port statement. `seq` tells apart two parcels of one commodity in one ship."""

    port: str                     # 'PQA' | 'KPT'
    statement_date: date
    section: str                  # berth | anchorage | expected | sailed | containers | programme | calls
    vessel: str                   # name as printed, prefix (M.V-, M.T-) removed
    call_ref: str = ""            # PQA registration no. / KPT registration no. ('' when not printed)
    direction: str = ""           # import | export | '' (not stated)
    commodity_raw: str = ""
    seq: int = 0
    commodity: str | None = None  # readable name (ports_pk.commodity)
    unit: str | None = None       # metric ton | cubic metre | teu | units
    berth_raw: str | None = None
    terminal: str | None = None
    agent_raw: str | None = None
    agent: str | None = None
    flag_raw: str | None = None
    flag: str | None = None
    port_raw: str | None = None   # loading port (PQA anchorage) / last port (expected)
    country: str | None = None    # country of that port
    vessel_type: str | None = None
    loa_m: float | None = None
    draft_m: float | None = None
    manifest: float | None = None
    handled_24h: float | None = None
    handled_before: float | None = None
    balance: float | None = None
    berthed_at: datetime | None = None
    arrived_on: date | None = None
    expected_at: datetime | None = None
    departs_at: datetime | None = None
    checks_ok: bool | None = None  # manifest = last 24 h + previous + balance (None: not checkable)
    details: dict = field(default_factory=dict)
    source_file: str | None = None

    def key(self) -> tuple:
        return (self.port, self.statement_date, self.section, self.vessel, self.call_ref, self.direction,
                self.commodity_raw, self.seq)


_COLS = ("port", "statement_date", "section", "vessel", "call_ref", "direction", "commodity_raw", "seq",
         "commodity", "unit", "berth_raw", "terminal", "agent_raw", "agent", "flag_raw", "flag", "port_raw",
         "country", "vessel_type", "loa_m", "draft_m", "manifest", "handled_24h", "handled_before", "balance",
         "berthed_at", "arrived_on", "expected_at", "departs_at", "checks_ok", "details", "source_file")
_KEY = ("port", "statement_date", "section", "vessel", "call_ref", "direction", "commodity_raw", "seq")
_VALUES = [c for c in _COLS if c not in _KEY and c != "source_file"]


def number_seqs(rows: list[VesselRow]) -> list[VesselRow]:
    """Give repeated keys (two parcels of one commodity in one ship) seq 1, 2 … in order. Pure."""
    seen: dict[tuple, int] = {}
    for r in rows:
        r.seq = 0
        k = r.key()
        r.seq = seen.get(k, 0)
        seen[k] = r.seq + 1
    return rows


def split(records: list) -> tuple[list, list[VesselRow]]:
    """(observation records, vessel rows). Pure."""
    rows = [r for r in records if isinstance(r, VesselRow)]
    return [r for r in records if not isinstance(r, VesselRow)], rows


def upsert_rows(rows: list[VesselRow], sections: tuple[str, ...] | None = None) -> int:
    """Write the rows, replacing each (port, statement date) wholesale: rows that date no longer
    lists are deleted, changed rows updated, unchanged rows left alone. `sections` limits the
    replacement to those sections, for a port whose sections come from different jobs (Karachi:
    the daily reports and the vessel history) — each job then removes only its own rows."""
    from app import db

    if not rows:
        return 0
    if sections is not None and {r.section for r in rows} - set(sections):
        raise ValueError(f"rows outside the sections this job replaces ({', '.join(sections)})")
    by_day: dict[tuple[str, date], dict[tuple, VesselRow]] = {}
    for r in rows:  # last one wins (files are read oldest → newest)
        by_day.setdefault((r.port, r.statement_date), {})[r.key()] = r
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _VALUES + ["source_file"])
    changed = " OR ".join(f"t.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in _VALUES)
    sql = (f"INSERT INTO port_vessel_calls AS t ({', '.join(_COLS)}) VALUES "
           f"({', '.join('%s::jsonb' if c == 'details' else '%s' for c in _COLS)}) "
           f"ON CONFLICT ({', '.join(_KEY)}) DO UPDATE SET {sets}, updated_at = now() WHERE {changed}")
    n = 0
    with db.connection() as conn:
        with conn.cursor() as cur:
            for (port, day), keyed in by_day.items():
                keep = [list(k[2:]) for k in keyed]
                cur.execute(
                    "DELETE FROM port_vessel_calls WHERE port = %s AND statement_date = %s AND "
                    + ("section = ANY(%s) AND " if sections is not None else "") +
                    "(section, vessel, call_ref, direction, commodity_raw, seq) NOT IN "
                    "(SELECT * FROM jsonb_to_recordset(%s::jsonb) AS k(section text, vessel text, call_ref text, "
                    "direction text, commodity_raw text, seq int))",
                    (port, day, *([list(sections)] if sections is not None else []),
                     json.dumps([dict(zip(_KEY[2:], k)) for k in keep])),
                )
                cur.executemany(sql, [tuple(json.dumps(r.details) if c == "details" else getattr(r, c)
                                            for c in _COLS) for r in keyed.values()])
                n += len(keyed)
    return n


class PortCallsMixin:
    """validate()/upsert() for jobs whose parse() returns vessel rows and observation records.
    `replace_sections`: set it when another job writes other sections of the same port (see upsert_rows)."""

    replace_sections: tuple[str, ...] | None = None

    def validate(self, records):
        obs, rows = split(records)
        return (super().validate(obs) if obs else []) + [(r, False) for r in rows]  # type: ignore[misc]

    def upsert(self, validated) -> int:
        obs = [(r, f) for r, f in validated if not isinstance(r, VesselRow)]
        rows = [r for r, _ in validated if isinstance(r, VesselRow)]
        return super().upsert(obs) + upsert_rows(rows, self.replace_sections)  # type: ignore[misc]
