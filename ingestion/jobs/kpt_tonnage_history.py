"""kpt_tonnage_history - Karachi Port's cargo and container traffic, every day and month since July 2020, from
KPT's Tonnage Report.

Source: Karachi Port Trust, "Cargo, Containers & Ship Handlings at Karachi Port Trust",
https://kpt.gov.pk/pages/86/tonnage-report?dtStart=YYYY-MM-DD&dtEnd=YYYY-MM-DD (other date formats
redirect to an error page). For any range it prints, in million tonnes to three decimals, imports, exports
and both together, each as bulk cargo, general cargo (containers included), total dry, total liquid and
total cargo, then TEUs imported and exported, in millions to three decimals. The report starts on
1 July 2020 (earlier ranges read 0).

A day D in the report is the 24 hours that Karachi Port's daily tonnage statement dated D+1 covers (to
07:30): the two agree to the thousand tonnes (Nov 2026: 104,497 t imported on the statement of the 10th,
0.104 Mt on the report's 9th), so values are dated D+1, the statement's date, and join the daily series
`kpt_shipping` keeps from the statements, to the tonne, from November 2026. Days the report prints all
zeros (today, before KPT enters it) are skipped. A day the daily job has already stored is never written
here (its exact figure wins).

Months come from monthly ranges (more precise than summing rounded days) for every complete month before
`kpt_shipping.LIVE_FROM`; `kpt_shipping` sums its own days for later months.

Where it runs: kpt.gov.pk answers only Pakistani residential traffic (timeout from the VPS, connection reset
on GitHub's runners, 522 through Cloudflare; probed 2026-10-31), so this is a one-off workstation job:
`ops/deploy/load_from_pc.sh kpt_tonnage_history --commit`. It is not scheduled — the live daily series
come from the statements the runners can read.
"""
from __future__ import annotations

import html
import json
import re
import time
from calendar import monthrange
from datetime import date, timedelta

from ingestion.framework import FetchedFile, IngestionJob, Record

REPORT_URL = "https://kpt.gov.pk/pages/86/tonnage-report"
FIRST_DAY = date(2020, 7, 1)
PAUSE_S = 0.5
BLOCKS = ("import", "export", "all")
KINDS = ("bulk", "general", "dry", "liquid", "total")


def figures(page: str) -> list[float] | None:
    """The report's 17 figures in order (5 imports, 5 exports, 5 both, TEUs in, TEUs out), or None. Pure."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", t))
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    i = next((k for k, x in enumerate(lines) if x.startswith("From :")), None)
    if i is None:
        return None
    nums = [float(x) for x in lines[i + 1:i + 80] if re.fullmatch(r"\d+(?:\.\d+)?", x)]
    return nums[:17] if len(nums) >= 17 else None


def to_records(when: date, nums: list[float], monthly: bool) -> list[Record]:
    """One report (a day, dated `when` = D+1, or a month, dated its last day) -> records. Pure."""
    sid = "ports.karachi.cargo_handled_monthly" if monthly else "ports.karachi.cargo_handled"
    out: list[Record] = []
    for b, block in enumerate(BLOCKS):
        for k, kind in enumerate(KINDS):
            v = round(nums[b * 5 + k] * 1e6)
            dims = {"level": "total", "direction": block} if kind == "total" else \
                {"level": "cargo_type", "direction": block, "cargo": kind}
            out.append(Record(sid, when, float(v), dims))
    teu_in, teu_out = round(nums[15] * 1e6), round(nums[16] * 1e6)
    for d, v in (("import", teu_in), ("export", teu_out), ("all", teu_in + teu_out)):
        if monthly:
            out.append(Record("ports.karachi.containers_teu_monthly", when, float(v), {"direction": d}))
        else:
            out.append(Record("ports.karachi.containers_teu", when, float(v),
                              {"level": "total", "direction": d, "cargo": "all"}))
    return out


class KptTonnageHistoryJob(IngestionJob):
    name = "kpt_tonnage_history"
    source = "KPT"

    def _get(self, a: date, b: date) -> list[float]:
        """The report's figures for a range. A page without them is a failure, not an empty day: KPT
        answers a long run of requests with error pages for minutes at a time (a first full run lost
        June 2023 → June 2025 that way), so back off and retry, then raise."""
        url = f"{REPORT_URL}?dtStart={a.isoformat()}&dtEnd={b.isoformat()}"
        for attempt in range(7):
            try:
                nums = figures(self.http_get(url).decode("utf-8", "replace"))
                if nums is not None:
                    return nums
            except Exception:  # noqa: BLE001
                pass
            time.sleep(min(180, 10 * 2 ** attempt))
        raise RuntimeError(f"tonnage report gave no figures for {a} → {b}")

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        from ingestion.jobs.kpt_shipping import LIVE_FROM

        today = date.today()
        days: dict[str, list[float]] = {}
        months: dict[str, list[float]] = {}
        d = FIRST_DAY
        missed: list[str] = []
        while d < today - timedelta(days=1):  # dated D+1: stop at yesterday, the live job writes today
            try:
                nums = self._get(d, d)
            except RuntimeError:
                missed.append(d.isoformat())
                nums = None
            if nums and any(nums):
                days[d.isoformat()] = nums
            d += timedelta(days=1)
            time.sleep(PAUSE_S)
        if len(missed) > 20:  # a long outage: fail loudly rather than load a history with holes
            raise RuntimeError(f"tonnage report gave no figures for {len(missed)} days ({missed[0]} …); re-run")
        m = FIRST_DAY
        while m < LIVE_FROM and date(m.year, m.month, monthrange(m.year, m.month)[1]) < today:
            end = date(m.year, m.month, monthrange(m.year, m.month)[1])
            nums = self._get(m, end)
            if nums and any(nums):
                months[end.isoformat()] = nums
            m = end + timedelta(days=1)
            time.sleep(PAUSE_S)
        if not days:
            raise ValueError("the tonnage report returned no figures for any day")
        body = json.dumps({"days": days, "months": months}).encode()
        return [FetchedFile(filename="kpt_tonnage_report.json", content=body, when=today)]

    def parse(self, f: FetchedFile) -> list[Record]:
        data = json.loads(f.content)
        out: list[Record] = []
        for day, nums in sorted(data["days"].items()):
            out += to_records(date.fromisoformat(day) + timedelta(days=1), nums, monthly=False)
        for end, nums in sorted(data["months"].items()):
            out += to_records(date.fromisoformat(end), nums, monthly=True)
        return out

    def validate(self, records):
        """Drop any point the daily job already stored (its figure, to the tonne, wins), then the usual gates."""
        from app import db

        ids = sorted({r.series_id for r in records})
        have = {(x["series_id"], x["obs_date"], json.dumps(x["dims"], sort_keys=True)) for x in db.query(
            "SELECT series_id, obs_date, dims FROM observations WHERE series_id = ANY(%(ids)s) AND value IS NOT NULL",
            {"ids": ids})}
        keep = [r for r in records if (r.series_id, r.obs_date, json.dumps(r.dims, sort_keys=True)) not in have]
        return super().validate(keep) if keep else []
