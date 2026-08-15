"""mufap_debt_prices - daily per-instrument prices for government debt securities.

Sourced from the same tab-46 file list as the yield curves. Three eras of layout
exist, so files are dispatched by CONTENT rather than filename (MUFAP shipped
sukuk data under PKRV-titled files at times):

  1. PKFRV<DDMMYYYY>.csv - floating-rate PIBs
         Bond Code,Price,Net Change
         PIBFR10YQ2030-10-22,98.71,-0.02

  2. Current PKISRV layout - GoP Ijara Sukuk in the left-hand columns
         FMA Code,SBP Code,Price,Change
         GOPIS-27-06-10-2026,GIS (FRR) -08,99.58,0.00
     (the right-hand columns carry the PKISRV curve, owned by mufap_pkrv)

  3. Legacy sukuk layouts
         A) broker-quote matrix   Name,,,AHL,C&M,...,Average
                                  GOPISV-30-04-2025,,,100.95,...,100.79
         B) Reuters revaluation   SECURITY,MATURITY,AHL,ALFA,...,AVG. RATE
                                  GOPIS-VRR,9-Oct-24, 100.10 ,..., 100.18
     Both quote several brokers plus a consensus average in the LAST column;
     that average is the price we store.

All three sukuk schemes encode the same two facts - rate type and maturity - so
they normalise to a single canonical code:

    GOPIS-<VRR|FRR>-<YYYY-MM-DD maturity>

with rate_type and maturity_date stored as first-class columns. That keeps one
instrument identity across eras instead of three aliases for the same security.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import date, datetime

from app import db
from app.config import settings
from ingestion.framework import FetchedFile, IngestionJob

LIST_URL = "https://www.mufap.com.pk/WebRegulations/GetSecpFileById"
FILE_BASE = "https://www.mufap.com.pk"
TAB_ID = 46
INCREMENTAL_FILES = 5

# Title prefixes to walk. PKRV is included because some legacy sukuk pricing was
# published under PKRV-titled files; pure curve files are skipped by the
# content dispatcher.
_SOURCES = ["PKFRV", "PKISRV", "PKRV"]

_MATURITY_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%Y-%m-%d")
# Current scheme: GOPIS-<issue no>-<dd-mm-yyyy maturity>
_MODERN_CODE_RE = re.compile(r"^GOPIS-(\d+)-(\d{1,2})-(\d{1,2})-(\d{4})$", re.I)
# Legacy A: GOPISV-/GOPISF-<dd-mm-yyyy>
_LEGACY_CODE_RE = re.compile(r"^GOPIS([VF])-(\d{1,2})-(\d{1,2})-(\d{4})$", re.I)
# Legacy B: a bare GOPIS-VRR / GOPIS-FRR with maturity in the next column
_REUTERS_SEC_RE = re.compile(r"^GOPIS[-\s]*(VRR|FRR)$", re.I)
_RATE_TYPE_RE = re.compile(r"\b(VRR|FRR)\b", re.I)


@dataclass
class PriceRow:
    code: str
    security_type: str
    sbp_code: str | None
    price: float | None
    net_change: float | None
    rate_type: str | None = None       # sukuk: 'VRR' | 'FRR'
    maturity_date: date | None = None


def _num(text) -> float | None:
    if text is None:
        return None
    t = str(text).strip().replace(",", "")
    if not t or t in {"-", "--", "N/A"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _parse_maturity(text: str) -> date | None:
    t = (text or "").strip()
    for fmt in _MATURITY_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _canonical(rate_type: str, maturity: date) -> str:
    return f"GOPIS-{rate_type.upper()}-{maturity:%Y-%m-%d}"


def _last_number(cells: list) -> float | None:
    """The consensus average sits in the last populated numeric column."""
    for cell in reversed(cells):
        v = _num(cell)
        if v is not None:
            return v
    return None


def _rows(text: str) -> list[list[str]]:
    return [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]


# ---- floating-rate PIBs ------------------------------------------------------

def parse_pkfrv_csv(text: str) -> list[PriceRow]:
    """Floating-rate PIB prices: Bond Code, Price, Net Change. Pure."""
    out: list[PriceRow] = []
    for row in _rows(text):
        code = (row[0] or "").strip()
        if not code or not re.match(r"^PIB", code, re.I):
            continue
        out.append(PriceRow(code=code, security_type="pib_floating", sbp_code=None,
                            price=_num(row[1]) if len(row) > 1 else None,
                            net_change=_num(row[2]) if len(row) > 2 else None))
    if not out:
        raise ValueError("no PIB floating-rate rows parsed from PKFRV CSV")
    return out


# ---- GoP Ijara Sukuk (three schemes, one canonical output) -------------------

def parse_pkisrv_instruments_csv(text: str) -> list[PriceRow]:
    """Current PKISRV layout. Strict on purpose: it requires the issue-numbered
    code AND an SBP code naming the rate type. Being permissive here previously
    swallowed the legacy layouts and stored broker quotes (and Excel date
    serials) in the sbp_code field. Pure."""
    out: list[PriceRow] = []
    for row in _rows(text):
        if len(row) < 3:
            continue
        m = _MODERN_CODE_RE.match((row[0] or "").strip())
        if not m:
            continue
        sbp = (row[1] or "").strip()
        rt = _RATE_TYPE_RE.search(sbp)
        if not rt:
            continue  # not this layout - let the legacy parsers try
        _seq, dd, mm, yyyy = m.groups()
        maturity = _parse_maturity(f"{yyyy}-{int(mm):02d}-{int(dd):02d}")
        price = _num(row[2])
        if maturity is None or price is None or price <= 0:
            continue
        rate_type = rt.group(1).upper()
        out.append(PriceRow(code=_canonical(rate_type, maturity),
                            security_type="gis_sukuk", sbp_code=sbp or None,
                            price=price,
                            net_change=_num(row[3]) if len(row) > 3 else None,
                            rate_type=rate_type, maturity_date=maturity))
    if not out:
        raise ValueError("no sukuk rows parsed from PKISRV CSV")
    return out


def parse_legacy_broker_quotes(text: str) -> list[PriceRow]:
    """Legacy A - broker-quote matrix keyed by GOPISV/GOPISF-<dd-mm-yyyy>. Pure."""
    out: list[PriceRow] = []
    for row in _rows(text):
        m = _LEGACY_CODE_RE.match((row[0] or "").strip())
        if not m:
            continue
        flag, dd, mm, yyyy = m.groups()
        maturity = _parse_maturity(f"{yyyy}-{int(mm):02d}-{int(dd):02d}")
        if maturity is None:
            continue
        price = _last_number(row[1:])
        if price is None or price <= 0:
            continue
        rate_type = "VRR" if flag.upper() == "V" else "FRR"
        out.append(PriceRow(code=_canonical(rate_type, maturity),
                            security_type="gis_sukuk", sbp_code=f"GIS ({rate_type})",
                            price=price, net_change=None,
                            rate_type=rate_type, maturity_date=maturity))
    if not out:
        raise ValueError("no legacy broker-quote rows parsed")
    return out


def parse_legacy_reuters(text: str) -> list[PriceRow]:
    """Legacy B - Reuters revaluation export (SECURITY + MATURITY columns). Pure."""
    out: list[PriceRow] = []
    for row in _rows(text):
        if len(row) < 3:
            continue
        m = _REUTERS_SEC_RE.match((row[0] or "").strip())
        if not m:
            continue
        maturity = _parse_maturity(row[1])
        if maturity is None:
            continue
        price = _last_number(row[2:])
        if price is None or price <= 0:
            continue
        rate_type = m.group(1).upper()
        out.append(PriceRow(code=_canonical(rate_type, maturity),
                            security_type="gis_sukuk", sbp_code=f"GIS ({rate_type})",
                            price=price, net_change=None,
                            rate_type=rate_type, maturity_date=maturity))
    if not out:
        raise ValueError("no legacy Reuters revaluation rows parsed")
    return out


def parse_debt_csv(text: str) -> list[PriceRow]:
    """Dispatch by content, not filename. Returns [] for pure yield-curve files
    (owned by mufap_pkrv) so they are skipped quietly rather than counted as
    failures."""
    head = text[:4000].lower()
    if re.search(r"^\s*tenor\s*,\s*mid rate", text, re.I | re.M) and "gopis" not in head:
        return []

    ordered = []
    if re.search(r"^\s*bond code", text, re.I | re.M) or "pibfr" in head:
        ordered.append(parse_pkfrv_csv)
    if "avg. rate" in head or "revaluation" in head:
        ordered.append(parse_legacy_reuters)
    if re.search(r"gopis[vf]-\d", head):
        ordered.append(parse_legacy_broker_quotes)
    # Strict current-layout parser, then the remaining fallbacks.
    ordered += [parse_pkisrv_instruments_csv, parse_legacy_reuters,
                parse_legacy_broker_quotes, parse_pkfrv_csv]

    tried = set()
    for fn in ordered:
        if fn in tried:
            continue
        tried.add(fn)
        try:
            return fn(text)
        except ValueError:
            continue
    return []


class MufapDebtPricesJob(IngestionJob):
    name = "mufap_debt_prices"
    source = "MUFAP"

    def _post_json(self, url: str, body: dict) -> str:
        from ingestion import http_client

        return http_client.post(url, json=body, timeout=45).text

    def _files_for(self, items: list[dict], prefix: str) -> list[tuple[date, str]]:
        rx = re.compile(rf"^{prefix}(\d{{8}})$")
        out: list[tuple[date, str]] = []
        for item in items:
            m = rx.match(str(item.get("Title", "")).strip())
            path = str(item.get("FilePath", ""))
            if not m or not path.lower().endswith(".csv"):
                continue
            try:
                d = datetime.strptime(m.group(1), "%d%m%Y").date()
            except ValueError:
                continue
            out.append((d, FILE_BASE + path))
        out.sort(key=lambda t: t[0], reverse=True)
        return out

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        items = json.loads(self._post_json(LIST_URL, {"fk_HeaderSubMenuTabId": TAB_ID}))["data"]
        out: list[FetchedFile] = []
        for prefix in _SOURCES:
            files = self._files_for(items, prefix)
            if not backfill:
                files = files[:INCREMENTAL_FILES]
            for d, url in files:
                try:
                    content = self.http_get(url)
                except Exception:
                    continue
                out.append(FetchedFile(filename=f"{prefix}{d:%d%m%Y}.csv",
                                       content=content, when=d))
        return out

    def parse(self, f: FetchedFile):
        """Returns (obs_date, PriceRow) pairs - this job writes dedicated
        securities tables, so it overrides validate/upsert below."""
        text = f.content.decode("utf-8", errors="replace")
        return [(f.when, r) for r in parse_debt_csv(text)]

    def validate(self, records):
        # Dedicated securities tables - the generic catalog gates do not apply.
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
                        INSERT INTO securities (code, security_type, sbp_code,
                                                rate_type, maturity_date,
                                                first_seen, last_seen, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                        ON CONFLICT (code) DO UPDATE SET
                            security_type=EXCLUDED.security_type,
                            sbp_code=COALESCE(EXCLUDED.sbp_code, securities.sbp_code),
                            rate_type=COALESCE(EXCLUDED.rate_type, securities.rate_type),
                            maturity_date=COALESCE(EXCLUDED.maturity_date,
                                                   securities.maturity_date),
                            first_seen=LEAST(securities.first_seen, EXCLUDED.first_seen),
                            last_seen=GREATEST(securities.last_seen, EXCLUDED.last_seen),
                            updated_at=now()
                        """,
                        (r.code, r.security_type, r.sbp_code, r.rate_type,
                         r.maturity_date, obs_date, obs_date),
                    )
                    cur.execute(
                        """
                        INSERT INTO security_prices (code, obs_date, price, net_change, revised_at)
                        VALUES (%s,%s,%s,%s, now())
                        ON CONFLICT (code, obs_date) DO UPDATE SET
                            price=EXCLUDED.price, net_change=EXCLUDED.net_change,
                            revised_at=now()
                        """,
                        (r.code, obs_date, r.price, r.net_change),
                    )
                    n += 1
        return n
