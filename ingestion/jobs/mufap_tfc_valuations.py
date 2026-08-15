"""mufap_tfc_valuations — daily valuation prices for corporate TFCs and Sukuks.

Source: MUFAP "Debt Instruments Rates" (file-list tab 44), a daily PDF titled
"Valuation of Debt Securities by MUFAP" published under SECP Master Circular 01
of 2023. Rows are grouped under rating buckets (GOVERNMENT GUARANTEED / AAA,
RATED AA+, ... RATED BBB):

    S.No. Code              Name of TFCs / Sukuks                 Traded/Non-Traded  Price
    1     BAHL/TFC/300921   BANK AL-HABIB LTD. - TFC (30-09-21)   Non-Traded         100.8311

pdfplumber's table extraction finds no ruled tables here, so rows are parsed from
text lines by shape. Written into the shared `securities` / `security_prices`
tables with security_type='tfc_sukuk' (verified: no priced line is missed).
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime

import pdfplumber

from app import db
from app.config import settings
from ingestion.framework import FetchedFile, IngestionJob

LIST_URL = "https://www.mufap.com.pk/WebRegulations/GetSecpFileById"
FILE_BASE = "https://www.mufap.com.pk"
TAB_ID = 44
INCREMENTAL_FILES = 3
SECURITY_TYPE = "tfc_sukuk"

_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\S+)\s+(.+?)\s+(Traded|Non-Traded|Non Traded)\s+([\d,]+\.\d+)\s*$"
)
# A rating bucket heading, e.g. 'RATED AA+' or 'GOVERNMENT GUARANTEED / AAA'.
_CAT_RE = re.compile(r"^[A-Z][A-Z0-9 /&\+\-(),\.]{4,}$")
_DATE_RE = re.compile(r"as of\s+([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})", re.I)


@dataclass
class TfcRow:
    code: str
    name: str
    rating_category: str | None
    traded_status: str
    price: float


def parse_tfc_pdf(pdf_bytes: bytes, fallback_date: date | None = None) -> tuple[date, list[TfcRow]]:
    """Parse a MUFAP debt-valuation PDF into (as_of_date, rows). Pure."""
    obs_date: date | None = None
    rows: list[TfcRow] = []
    category: str | None = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if obs_date is None:
                m = _DATE_RE.search(text)
                if m:
                    for fmt in ("%b %d, %Y", "%B %d, %Y"):
                        try:
                            obs_date = datetime.strptime(re.sub(r"\s+", " ", m.group(1)), fmt).date()
                            break
                        except ValueError:
                            continue
            for line in text.splitlines():
                m = _ROW_RE.match(line)
                if m:
                    _sno, code, name, traded, price = m.groups()
                    rows.append(TfcRow(
                        code=code.strip(), name=name.strip(),
                        rating_category=category,
                        traded_status=traded.replace(" ", "-"),
                        price=float(price.replace(",", "")),
                    ))
                    continue
                stripped = line.strip()
                if _CAT_RE.match(stripped) and "VALUATION" not in stripped:
                    category = stripped

    if not rows:
        raise ValueError("no TFC/Sukuk valuation rows parsed")
    return (obs_date or fallback_date or date.today()), rows


class MufapTfcValuationsJob(IngestionJob):
    name = "mufap_tfc_valuations"
    source = "MUFAP"

    def _post_json(self, url: str, body: dict) -> str:
        from ingestion import http_client

        return http_client.post(url, json=body, timeout=45).text

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        items = json.loads(self._post_json(LIST_URL, {"fk_HeaderSubMenuTabId": TAB_ID}))["data"]
        pdfs = [i for i in items if str(i.get("FilePath", "")).lower().endswith(".pdf")]
        # The list is oldest-first; take the newest for an incremental run.
        pdfs = pdfs if backfill else pdfs[-INCREMENTAL_FILES:]
        out: list[FetchedFile] = []
        for item in reversed(pdfs):
            try:
                content = self.http_get(FILE_BASE + item["FilePath"])
            except Exception:
                continue
            if content[:4] != b"%PDF":
                continue
            out.append(FetchedFile(filename=str(item.get("Title", "tfc")).strip() + ".pdf",
                                   content=content, when=date.today()))
        return out

    def parse(self, f: FetchedFile):
        obs_date, rows = parse_tfc_pdf(f.content, f.when)
        return [(obs_date, r) for r in rows]

    def validate(self, records):
        # Dedicated securities tables — the generic catalog gates don't apply.
        return [(r, False) for r in records]

    def upsert(self, validated) -> int:
        pairs = [r for r, _ in validated]
        if not pairs:
            return 0
        n = 0
        with db.connection() as conn:
            with conn.cursor() as cur:
                for obs_date, r in pairs:
                    cur.execute(
                        """
                        INSERT INTO securities (code, security_type, name, rating_category,
                                                first_seen, last_seen, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (code) DO UPDATE SET
                            security_type=EXCLUDED.security_type,
                            name=COALESCE(EXCLUDED.name, securities.name),
                            rating_category=COALESCE(EXCLUDED.rating_category, securities.rating_category),
                            first_seen=LEAST(securities.first_seen, EXCLUDED.first_seen),
                            last_seen=GREATEST(securities.last_seen, EXCLUDED.last_seen),
                            updated_at=now()
                        """,
                        (r.code, SECURITY_TYPE, r.name, r.rating_category, obs_date, obs_date),
                    )
                    cur.execute(
                        """
                        INSERT INTO security_prices (code, obs_date, price, traded_status, revised_at)
                        VALUES (%s,%s,%s,%s, now())
                        ON CONFLICT (code, obs_date) DO UPDATE SET
                            price=EXCLUDED.price, traded_status=EXCLUDED.traded_status,
                            revised_at=now()
                        """,
                        (r.code, obs_date, r.price, r.traded_status),
                    )
                    n += 1
        return n
