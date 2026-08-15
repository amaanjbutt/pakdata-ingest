"""sbp_policy_rate — SBP policy rate and interest-rate corridor.

Source: https://www.sbp.org.pk/our-operations/monetary-policy
The page shows the current corridor as a labelled block:
    SBP Overnight Repo Rate (Floor) Rate      10.50% p.a.
    SBP Overnight Reverse Repo (Ceiling Rate)  12.50% p.a.
    SBP Policy Rate                            11.50% p.a.

The rate changes only on MPC decisions (~every 6 weeks), and the page carries no
effective date, so we use **change detection**: each run reads the current rates
and writes an observation *only when a value differs from the last stored one*,
dated the run day. A run with no change upserts nothing (status success, 0 rows).

The 2026 SBP site redesign removed the historical rate table, so `--backfill`
has no archive to walk (no-op). Three series: `policy_rate` (headline),
`policy_rate.floor` (repo), `policy_rate.ceiling` (reverse repo).
"""
from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from app import db
from ingestion.framework import FetchedFile, IngestionJob, Record

POLICY_URL = "https://www.sbp.org.pk/our-operations/monetary-policy"

# series id -> label-anchored extraction pattern (number must be adjacent to the
# label, so prose mentions of "policy rate" elsewhere can't match).
_PATTERNS = {
    "rates.policy": r"SBP Policy Rate[^%]{0,20}?(\d{1,2}\.\d{2})\s*%",
    "rates.policy.floor": r"Repo Rate \(Floor\)[^%]{0,25}?(\d{1,2}\.\d{2})\s*%",
    "rates.policy.ceiling": r"Reverse Repo \(Ceiling[^%]{0,25}?(\d{1,2}\.\d{2})\s*%",
}


def parse_policy_html(html: str) -> dict[str, float]:
    """Extract {series_id: rate} for the current corridor. Pure — no DB/network."""
    text = re.sub(r"\s+", " ", BeautifulSoup(html, "lxml").get_text(" "))
    out: dict[str, float] = {}
    for sid, pat in _PATTERNS.items():
        m = re.search(pat, text, re.I)
        if m:
            out[sid] = float(m.group(1))
    if "rates.policy" not in out:
        raise ValueError("policy rate not found on monetary-policy page")
    return out


class SbpPolicyRateJob(IngestionJob):
    name = "sbp_policy_rate"
    source = "SBP"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        content = self.http_get(POLICY_URL)
        return [FetchedFile(filename="monetary_policy.html", content=content, when=date.today())]

    def _last_value(self, series_id: str) -> float | None:
        row = db.query_one(
            "SELECT value FROM observations WHERE series_id=%s "
            "ORDER BY obs_date DESC LIMIT 1",
            (series_id,),
        )
        return float(row["value"]) if row and row["value"] is not None else None

    def parse(self, f: FetchedFile) -> list[Record]:
        current = parse_policy_html(f.content.decode("utf-8", errors="replace"))
        records: list[Record] = []
        for sid, val in current.items():
            last = self._last_value(sid)
            if last is None or abs(last - val) > 1e-9:  # new or changed
                records.append(Record(sid, f.when, val, {}))
        return records
