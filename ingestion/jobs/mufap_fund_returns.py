"""mufap_fund_returns — daily mutual-fund performance/returns (MUFAP).

Source: https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=1 ("Performance
Summary"). Server-rendered table, one row per fund (FundID via its link), with
rating, benchmark, and trailing returns: YTD, MTD, 1/15/30/90/180/270/365-day.

Stored in `fund_returns` keyed (fund_id, obs_date=Validity Date). Accrues daily.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup

from app import db
from ingestion import alerting, storage
from ingestion.framework import IngestionJob

RETURNS_URL = "https://www.mufap.com.pk/Industry/IndustryStatDaily?tab=1"
_FUNDID_RE = re.compile(r"FundID=(\d+)")
# Return columns in page order after: Sector, Category, Fund, Rating, Benchmark,
# Validity Date, NAV, then the trailing-return columns below.
_RETURN_COLS = ["ytd", "mtd", "d1", "d15", "d30", "d90", "d180", "d270", "d365"]


@dataclass
class ReturnRow:
    fund_id: int
    obs_date: date | None
    rating: str | None
    benchmark: str | None
    returns: dict[str, float | None]


def _num(text: str) -> float | None:
    t = (text or "").strip().replace(",", "").rstrip("%")
    if not t or t in {"-", "--", "N/A", "n/a"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _date(text: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d-%b-%Y"):
        try:
            return datetime.strptime((text or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_returns_html(html: str) -> list[ReturnRow]:
    """Parse the performance table into ReturnRow records. Pure — no DB/network."""
    soup = BeautifulSoup(html, "lxml")
    target = None
    for t in soup.find_all("table"):
        heads = [c.get_text(strip=True).lower() for c in t.find_all("th")]
        if "ytd" in heads and "benchmark" in heads:
            target = t
            break
    if target is None:
        raise ValueError("performance table not found")

    out: list[ReturnRow] = []
    for tr in target.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 15:
            continue
        link = tr.find("a", href=_FUNDID_RE)
        if not link:
            continue
        fund_id = int(_FUNDID_RE.search(link["href"]).group(1))
        c = [td.get_text(" ", strip=True) for td in tds]
        # Columns: 0 Sector,1 Category,2 Fund,3 Rating,4 Benchmark,5 Validity,6 NAV,7.. returns
        returns = {}
        for i, key in enumerate(_RETURN_COLS):
            idx = 7 + i
            returns[key] = _num(c[idx]) if idx < len(c) else None
        out.append(ReturnRow(
            fund_id=fund_id, obs_date=_date(c[5]),
            rating=c[3] or None, benchmark=c[4] or None, returns=returns,
        ))
    if not out:
        raise ValueError("no fund rows parsed from performance table")
    return out


class MufapFundReturnsJob(IngestionJob):
    name = "mufap_fund_returns"
    source = "MUFAP"

    def run(self, backfill: bool = False) -> dict:
        run_id = self._start_run()
        try:
            content = self.http_get(RETURNS_URL)
            raw = storage.archive(self.source, self.name, date.today(), "returns.html", content)
            rows = parse_returns_html(content.decode("utf-8", errors="replace"))
            n = self._upsert(rows)
            self._finish(run_id, "success", n, None, raw)
            return {"status": "success", "rows": n, "funds": len(rows)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc), None)
            alerting.alert(f"{self.name}: job failed", str(exc))
            raise

    def _upsert(self, rows: list[ReturnRow]) -> int:
        n = 0
        # A fund can appear in the returns feed before it exists in `funds` (the
        # NAV snapshot registers funds). Skip returns for funds we don't yet know,
        # rather than let one FK violation abort the whole batch.
        known = {
            row["fund_id"]
            for row in db.query("SELECT fund_id FROM funds")
        }
        with db.connection() as conn:
            with conn.cursor() as cur:
                for r in rows:
                    if r.obs_date is None or r.fund_id not in known:
                        continue
                    rt = r.returns
                    cur.execute(
                        """
                        INSERT INTO fund_returns (fund_id, obs_date, rating, benchmark,
                            ytd, mtd, d1, d15, d30, d90, d180, d270, d365, revised_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (fund_id, obs_date) DO UPDATE SET
                            rating=EXCLUDED.rating, benchmark=EXCLUDED.benchmark,
                            ytd=EXCLUDED.ytd, mtd=EXCLUDED.mtd, d1=EXCLUDED.d1,
                            d15=EXCLUDED.d15, d30=EXCLUDED.d30, d90=EXCLUDED.d90,
                            d180=EXCLUDED.d180, d270=EXCLUDED.d270, d365=EXCLUDED.d365,
                            revised_at=now()
                        """,
                        (r.fund_id, r.obs_date, r.rating, r.benchmark,
                         rt["ytd"], rt["mtd"], rt["d1"], rt["d15"], rt["d30"],
                         rt["d90"], rt["d180"], rt["d270"], rt["d365"]),
                    )
                    n += 1
        return n
