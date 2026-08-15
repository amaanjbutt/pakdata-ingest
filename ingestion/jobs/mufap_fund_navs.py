"""mufap_fund_navs — daily NAVs for every mutual fund in Pakistan (MUFAP).

Source: https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=3 ("NAVs and Sale
Loads"). Server-rendered — a single HTML table of ~538 funds, each row carrying a
stable MUFAP FundID (via its FundDetail link) plus AMC, category, sector,
inception date, and the day's Offer / Repurchase / NAV and front/back-end loads.
The NAV date is the row's "Validity Date".

Modelled with dedicated `funds` + `fund_navs` tables (not ~1,600 generic series).
Each daily run upserts the fund metadata and appends that day's NAV row.

`--backfill` pulls full history: one POST per fund to
`/AMC/GetFundDetailbyAMCByDate` ({FundID, Date}) returns that fund's entire NAV
series (nested Table1: entryDate, netval — often 7,000+ points back to inception).
Resumable (skips funds already backfilled); historical points carry NAV only and
never overwrite offer/repurchase captured by the daily snapshot.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

from app import db
from app.config import settings
from ingestion import alerting, storage
from ingestion.framework import IngestionJob

NAV_URL = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=3"
HISTORY_URL = "https://www.mufap.com.pk/AMC/GetFundDetailbyAMCByDate"
# A fund is considered already-backfilled if it has at least this many NAV rows
# (so a resumed --backfill skips funds it already pulled full history for).
_BACKFILLED_MIN_ROWS = 30
_FUNDID_RE = re.compile(r"FundID=(\d+)")
_HEADERS = ["sector", "amc", "fund", "category", "inception date", "offer",
            "repurchase", "nav", "validity date"]


@dataclass
class FundRow:
    fund_id: int
    sector: str
    amc: str
    name: str
    category: str
    inception_date: date | None
    offer: float | None
    repurchase: float | None
    nav: float | None
    obs_date: date | None
    front_end: float | None
    back_end: float | None
    trustee: str | None


def _num(text: str) -> float | None:
    t = (text or "").strip().replace(",", "")
    if not t or t in {"-", "--", "N/A"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _date(text: str) -> date | None:
    t = (text or "").strip()
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def parse_nav_html(html: str) -> list[FundRow]:
    """Parse the NAV table into FundRow records. Pure — no DB/network."""
    soup = BeautifulSoup(html, "lxml")
    target = None
    for t in soup.find_all("table"):
        heads = [c.get_text(strip=True).lower() for c in t.find_all("th")]
        if "nav" in heads and "fund" in heads:
            target = t
            break
    if target is None:
        raise ValueError("NAV table not found on MUFAP page")

    out: list[FundRow] = []
    for tr in target.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue
        link = tr.find("a", href=_FUNDID_RE)
        if not link:
            continue
        fund_id = int(_FUNDID_RE.search(link["href"]).group(1))
        c = [td.get_text(" ", strip=True) for td in tds]
        out.append(FundRow(
            fund_id=fund_id, sector=c[0], amc=c[1], name=c[2], category=c[3],
            inception_date=_date(c[4]), offer=_num(c[5]), repurchase=_num(c[6]),
            nav=_num(c[7]), obs_date=_date(c[8]),
            front_end=_num(c[9]) if len(c) > 9 else None,
            back_end=_num(c[10]) if len(c) > 10 else None,
            trustee=c[13] if len(c) > 13 else None,
        ))
    if not out:
        raise ValueError("no fund rows parsed from NAV table")
    return out


def parse_nav_history(json_text: str) -> list[tuple[date, float]]:
    """Parse a GetFundDetailbyAMCByDate response into [(date, nav)]. The NAV series
    is the nested Table1 (entryDate, netval). Pure — no DB/network."""
    outer = json.loads(json_text)
    inner = json.loads(outer["data"]) if isinstance(outer.get("data"), str) else outer.get("data") or {}
    out: list[tuple[date, float]] = []
    for x in inner.get("Table1", []):
        raw_d = str(x.get("entryDate") or "")[:10]
        try:
            d = datetime.strptime(raw_d, "%Y-%m-%d").date()
        except ValueError:
            continue
        v = x.get("netval")
        if v is None:
            continue
        try:
            out.append((d, float(v)))
        except (TypeError, ValueError):
            continue
    return out


class MufapFundNavsJob(IngestionJob):
    name = "mufap_fund_navs"
    source = "MUFAP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            content = self.http_get(NAV_URL)
            raw = storage.archive(self.source, self.name, date.today(), "navs.html", content)
            rows = parse_nav_html(content.decode("utf-8", errors="replace"))
            n = self._upsert_funds(rows)
            extra = {}
            if backfill:
                extra = self._backfill_history(rows)
            self._finish(run_id, "success", n + extra.get("hist_rows", 0), None, raw)
            return {"status": "success", "rows": n, "funds": len(rows), **extra}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            alerting.alert(f"{self.name}: job failed", str(exc))
            raise

    # ---- historical backfill ------------------------------------------------

    def _funds_needing_history(self, fund_ids: list[int]) -> list[int]:
        rows = db.query(
            "SELECT fund_id, count(*) AS n FROM fund_navs "
            "WHERE fund_id = ANY(%(ids)s) GROUP BY fund_id",
            {"ids": fund_ids},
        )
        have = {r["fund_id"] for r in rows if r["n"] >= _BACKFILLED_MIN_ROWS}
        return [fid for fid in fund_ids if fid not in have]

    def _fetch_history(self, fund_id: int) -> list[tuple[date, float]]:
        from ingestion import http_client

        resp = http_client.post(
            HISTORY_URL,
            json={"FundID": fund_id, "Date": date.today().strftime("%m/%d/%Y")},
            timeout=60,
        )
        return parse_nav_history(resp.text)

    def _backfill_history(self, rows: list[FundRow]) -> dict:
        """One call per fund returns its full NAV history (thousands of points).
        Resumable: funds that already have history are skipped. Historical points
        carry NAV only; existing offer/repurchase from the daily snapshot are
        preserved (not overwritten with NULL)."""
        todo = self._funds_needing_history([r.fund_id for r in rows])
        hist_rows = 0
        done = 0
        for fund_id in todo:
            try:
                series = self._fetch_history(fund_id)
            except Exception as exc:  # skip a bad fund, keep going
                alerting.alert(f"{self.name}: history fetch failed", f"fund {fund_id}: {exc}")
                continue
            hist_rows += self._upsert_history(fund_id, series)
            done += 1
            time.sleep(0.3)  # be a good citizen
        return {"hist_rows": hist_rows, "funds_backfilled": done}

    def _upsert_history(self, fund_id: int, series: list[tuple[date, float]]) -> int:
        if not series:
            return 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO fund_navs (fund_id, obs_date, nav, revised_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (fund_id, obs_date) DO UPDATE SET
                        nav = EXCLUDED.nav, revised_at = now()
                    """,
                    [(fund_id, d, v) for d, v in series],
                )
        return len(series)

    def _upsert_funds(self, rows: list[FundRow]) -> int:
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    cur.execute(
                        """
                        INSERT INTO funds (fund_id, name, amc, sector, category,
                                           inception_date, trustee, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (fund_id) DO UPDATE SET
                            name=EXCLUDED.name, amc=EXCLUDED.amc, sector=EXCLUDED.sector,
                            category=EXCLUDED.category, inception_date=EXCLUDED.inception_date,
                            trustee=EXCLUDED.trustee, is_active=true, updated_at=now()
                        """,
                        (r.fund_id, r.name, r.amc, r.sector, r.category,
                         r.inception_date, r.trustee),
                    )
                    if r.obs_date is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO fund_navs (fund_id, obs_date, nav, offer, repurchase,
                                               front_end, back_end, revised_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (fund_id, obs_date) DO UPDATE SET
                            nav=EXCLUDED.nav, offer=EXCLUDED.offer,
                            repurchase=EXCLUDED.repurchase, front_end=EXCLUDED.front_end,
                            back_end=EXCLUDED.back_end, revised_at=now()
                        """,
                        (r.fund_id, r.obs_date, r.nav, r.offer, r.repurchase,
                         r.front_end, r.back_end),
                    )
                    n += 1
        return n
