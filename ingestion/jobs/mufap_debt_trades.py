"""mufap_debt_trades — trade-level secondary-market debt transactions.

Source: MUFAP "Debt Instruments Daily Trading" (file-list tab 45). Each PDF is a
running month-to-date list of reported trades in TFCs, Sukuks, PIBs and other debt
instruments:

    BATS / Non-BATS | Trade Date | Issue Name (+ nature) | Issue Date | Maturity |
    Listed/Unlisted | Face Value (per cert) | Trade Volume (units) |
    Trade Value (Rs Mn) | Trade Price (% of FV)

pdfplumber finds no ruled tables, so rows are parsed from text lines. The source
inserts spurious spaces inside numbers ("4 ,995.00", "9 8.40"); the four trailing
numbers are split on the boundary that follows each `.dd`, then de-spaced. Stored
in `debt_trades`; a natural-key UNIQUE makes overlapping monthly files idempotent.
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime

import pdfplumber

from app import db
from ingestion.framework import FetchedFile, IngestionJob

LIST_URL = "https://www.mufap.com.pk/WebRegulations/GetSecpFileById"
FILE_BASE = "https://www.mufap.com.pk"
TAB_ID = 45
INCREMENTAL_FILES = 2  # newest month-to-date file(s); dedup handles overlap

# Case-insensitive: since mid-2026 MUFAP prints "Non-Bats" (the old upper-case-only
# pattern silently dropped every such trade) and the occasional "Unliisted" typo.
_ROW_RE = re.compile(
    r"^(BATS|Non-BATS)\s+"
    r"(\d{1,2}-[A-Za-z]{3}-\d{2})\s+"
    r"(.+?)\s+"
    r"([A-Za-z]{3}\s+\d{1,2},\s*\d{4})\s+"
    r"([A-Za-z]{3}\s+\d{1,2},\s*\d{4})\s+"
    r"(Listed|Un-?l+i+sted)\s+"
    r"(.+)$",
    re.I,
)
# Split the trailing run of numbers on the space that follows a "…dd" number end.
_NUM_SPLIT = re.compile(r"(?<=\.\d\d)\s+")


@dataclass
class TradeRow:
    trade_date: date
    bats: str
    issue_name: str
    issue_date: date | None
    maturity_date: date | None
    listed: str
    face_value: float | None
    volume: float | None
    value_mn: float | None
    price_pct: float | None


def _num(s: str) -> float | None:
    s = s.replace(" ", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _date(s: str, fmts: tuple[str, ...]) -> date | None:
    s = re.sub(r"\s+", " ", s.strip())
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _page_lines(page) -> list[str]:
    """Text lines rebuilt from word positions, joining words that touch. The PDFs set
    some numbers in pieces with no gap between them — extract_text() reads a volume of
    25.00 as "2 5.00" and 1,500.00 as "1 ,500.00", which shifts every column after it."""
    words = sorted(page.extract_words(x_tolerance=1.5), key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1][0]["top"]) < 2.5:
            lines[-1].append(w)
        else:
            lines.append([w])
    out = []
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
        text = ln[0]["text"]
        for prev, w in zip(ln, ln[1:]):
            text += ("" if w["x0"] - prev["x1"] < 0.5 else " ") + w["text"]
        out.append(text)
    return out


def _numbers(tail: str) -> list[float | None]:
    """Face value, volume, value (Rs mn), price (% of FV) from the trailing columns."""
    parts = tail.split()
    if len(parts) != 4:  # older layouts: split only after a "…dd" decimal end
        parts = _NUM_SPLIT.split(tail.strip())
    return ([_num(x) for x in parts] + [None, None, None, None])[:4]


def parse_debt_trades(pdf_bytes: bytes) -> list[TradeRow]:
    """Parse a MUFAP daily-trading PDF into TradeRow records. Pure."""
    rows: list[TradeRow] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for line in _page_lines(page):
                m = _ROW_RE.match(line.strip())
                if not m:
                    continue
                bats, td, issue, idate, mdate, listed, tail = m.groups()
                bats = "BATS" if bats.upper() == "BATS" else "Non-BATS"
                listed = "Listed" if listed.lower() == "listed" else "Unlisted"
                nums = _numbers(tail)
                trade_date = _date(td, ("%d-%b-%y",))
                if trade_date is None:
                    continue
                rows.append(TradeRow(
                    trade_date=trade_date,
                    bats=bats,
                    issue_name=re.sub(r"\s+", " ", issue).strip(),
                    issue_date=_date(idate, ("%b %d, %Y", "%B %d, %Y")),
                    maturity_date=_date(mdate, ("%b %d, %Y", "%B %d, %Y")),
                    listed=listed,
                    face_value=nums[0], volume=nums[1], value_mn=nums[2], price_pct=nums[3],
                ))
    return rows


class MufapDebtTradesJob(IngestionJob):
    name = "mufap_debt_trades"
    source = "MUFAP"

    def _post_json(self, url: str, body: dict) -> str:
        from ingestion import http_client

        return http_client.post(url, json=body, timeout=45).text

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        items = json.loads(self._post_json(LIST_URL, {"fk_HeaderSubMenuTabId": TAB_ID}))["data"]
        if isinstance(items, str):
            items = json.loads(items)
        pdfs = [i for i in items if str(i.get("FilePath", "")).lower().endswith(".pdf")]
        pdfs = pdfs if backfill else pdfs[-INCREMENTAL_FILES:]
        out: list[FetchedFile] = []
        for item in pdfs:
            fp = str(item.get("FilePath", ""))
            url = fp if fp.startswith("http") else FILE_BASE + "/" + fp.lstrip("/")
            try:
                content = self.http_get(url)
            except Exception:
                continue
            if content[:4] != b"%PDF":
                continue
            out.append(FetchedFile(filename=str(item.get("Title", "trades")).strip() + ".pdf",
                                   content=content, when=date.today()))
        return out

    def parse(self, f: FetchedFile):
        return parse_debt_trades(f.content)

    def validate(self, records):
        # Dedicated table — the generic catalog gates don't apply.
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        rows = [r for r, _ in validated]
        if not rows:
            return 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO debt_trades (trade_date, bats, issue_name, issue_date,
                        maturity_date, listed, face_value, volume, value_mn, price_pct)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (trade_date, bats, issue_name, maturity_date, volume, value_mn, price_pct)
                    DO NOTHING
                    """,
                    [(r.trade_date, r.bats, r.issue_name, r.issue_date, r.maturity_date,
                      r.listed, r.face_value, r.volume, r.value_mn, r.price_pct) for r in rows],
                )
        return len(rows)
