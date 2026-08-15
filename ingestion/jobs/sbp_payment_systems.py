"""sbp_payment_systems - SBP Payment Systems Quarterly Review (Phase 6).

Source: the "Payment Systems Review" quarterly PDF, published on the redesigned
SBP site under
  /our-operations/publications/payment-systems-review/365
which links to per-fiscal-year subpages, each listing the quarter PDFs under
/assets/document/publications/... (real PDFs; the legacy /psd/pdf/ path now
returns the redesign catch-all and is dead).

We parse the "SNAPSHOT OF PAYMENT SYSTEMS" page (annexure summary), which is a
stable two-part table carrying, for the two most recent quarter-ends:
  - Infrastructure & user counts (ATMs, POS, cards, EMIs, PSOs, BB agents,
    e-commerce/QR merchants, digital-channel users, ...),
  - A payments summary (PRISM + retail, split digital/OTC) as volume & value.

Each PDF carries the current AND prior quarter, so parsing the latest each
quarter accumulates full history; `--backfill` walks older fiscal-year pages
(older/off-format PDFs are skipped by the framework's per-file resilience).

This is Pakistan's core digital-payments dataset (Raast/PRISM/POS/cards/e-commerce)
— genuinely absent from EasyData.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

from ingestion.framework import Record
from ingestion.pdf_report import PdfReportJob

LANDING = "https://www.sbp.org.pk/our-operations/publications/payment-systems-review/365"
_FY_SLUG = r"payment-systems-quarterly-reports-(\d{4})-\d{4}"
_MIN_FY = 2018  # older PDFs pre-date the stable snapshot layout


class Series:
    __slots__ = ("id", "name", "unit", "min_value", "max_value")

    def __init__(self, sid, name, unit, min_value, max_value):
        self.id, self.name, self.unit = sid, name, unit
        self.min_value, self.max_value = min_value, max_value


# --- SNAPSHOT infrastructure/user counts: line prefix -> series -------------
# Order matters: the first prefix a line starts with wins, so more specific
# prefixes precede overlapping shorter ones.
_INFRA: list[tuple[str, Series]] = [
    ("Currency in Circulation", Series("payments.currency_in_circulation", "Currency in Circulation", "pkr_bn", 0, 200000)),
    ("Banks, Microfinance Banks", Series("payments.banks_count", "Banks, MFBs & Digital Banks (count)", "count", 0, 500)),
    ("Payment System Operators", Series("payments.psos_psps_count", "Payment System Operators/Providers (count)", "count", 0, 500)),
    ("Electronic Money Institutions", Series("payments.emis_count", "Electronic Money Institutions (count)", "count", 0, 500)),
    ("Branchless Banking Service Providers", Series("payments.bb_providers_count", "Branchless Banking Providers (count)", "count", 0, 500)),
    ("PRISM Participants", Series("payments.prism_participants", "PRISM Participants (count)", "count", 0, 500)),
    ("Branches of Banks", Series("payments.bank_branches", "Bank & MFB Branches", "count", 0, 200000)),
    ("Branchless Banking Agents", Series("payments.bb_agents", "Branchless Banking Agents", "count", 0, 10000000)),
    ("ATMs", Series("payments.atms", "ATMs", "count", 0, 200000)),
    ("CDMs", Series("payments.cdms", "CDMs/CCDMs", "count", 0, 100000)),
    ("Point-of-Sale", Series("payments.pos_machines", "Point-of-Sale (POS) Machines", "count", 0, 5000000)),
    ("PoS enabled Merchants", Series("payments.pos_merchants", "POS-enabled Merchants", "count", 0, 5000000)),
    ("Registered E-Commerce Merchants", Series("payments.ecommerce_merchants", "Registered E-Commerce Merchants", "count", 0, 2000000)),
    ("QR enabled Merchants", Series("payments.qr_merchants", "QR-enabled Merchants", "count", 0, 50000000)),
    ("BB Mobile App Users", Series("payments.bb_app_users", "Branchless Banking App Users", "million", 0, 2000)),
    ("Mobile Banking Users", Series("payments.mobile_banking_users", "Mobile Banking App Users", "million", 0, 2000)),
    ("EMIs' E-Wallet App Users", Series("payments.ewallet_users", "EMI E-Wallet App Users", "million", 0, 2000)),
    ("EMIs’ E-Wallet App Users", Series("payments.ewallet_users", "EMI E-Wallet App Users", "million", 0, 2000)),
    ("Internet Banking Users", Series("payments.internet_banking_users", "Internet Banking Users", "million", 0, 2000)),
    ("Call Center", Series("payments.ivr_users", "Call Center/IVR Banking Users", "million", 0, 2000)),
    ("Payment Cards", Series("payments.payment_cards", "Payment Cards", "million", 0, 2000)),
]

# --- SNAPSHOT payments summary: line prefix -> (volume series, value series) --
_PAY: list[tuple[str, Series, Series]] = [
    ("RTGS", Series("payments.prism.volume", "PRISM (RTGS) Transactions - Volume", "million", 0, 100000),
             Series("payments.prism.value", "PRISM (RTGS) Transactions - Value", "pkr_tn", 0, 5000)),
    ("Retail Payments", Series("payments.retail.volume", "Retail Payments - Volume", "million", 0, 100000),
                        Series("payments.retail.value", "Retail Payments - Value", "pkr_tn", 0, 5000)),
    ("Digital Channels", Series("payments.retail.digital.volume", "Retail Payments (Digital) - Volume", "million", 0, 100000),
                         Series("payments.retail.digital.value", "Retail Payments (Digital) - Value", "pkr_tn", 0, 5000)),
    ("OTC Channels", Series("payments.retail.otc.volume", "Retail Payments (OTC) - Volume", "million", 0, 100000),
                     Series("payments.retail.otc.value", "Retail Payments (OTC) - Value", "pkr_tn", 0, 5000)),
]

# ============================================================================
# ANNEXURES — richer 5-quarter time series (deeper history than the 2-quarter
# snapshot). Each annexure is a table with a Q?-FY?? header spanning ~5 quarters.
# We parse the highest-value ones that map to competitor coverage.
# ============================================================================

def _vv(base: str, name: str, vmax_vol: float, vmax_val: float) -> tuple[Series, Series]:
    return (
        Series(f"{base}.volume", f"{name} - Volume", "million", 0, vmax_vol),
        Series(f"{base}.value", f"{name} - Value", "pkr_bn", 0, vmax_val),
    )


# A-2 Composition of Payment Cards (counts, one value per quarter).
_A2_CARDS: dict[str, Series] = {
    "Credit Cards": Series("payments.cards.credit", "Credit Cards Issued", "count", 0, 100000000),
    "Debit Cards": Series("payments.cards.debit", "Debit Cards Issued", "count", 0, 500000000),
    "Pre-Paid Cards": Series("payments.cards.prepaid", "Pre-Paid Cards Issued", "count", 0, 100000000),
    "Social Welfare Cards": Series("payments.cards.social_welfare", "Social Welfare Cards Issued", "count", 0, 100000000),
}

# A-4 Retail Value Payments by channel (Volume mn, Value PKR bn).
_A4_CHANNELS: dict[str, tuple[Series, Series]] = {
    "ATM/CDM": _vv("payments.channel.atm", "ATM/CDM Transactions", 5000, 200000),
    "POS": _vv("payments.channel.pos", "POS Transactions", 5000, 50000),
    "Internet Banking": _vv("payments.channel.internet", "Internet Banking Transactions", 5000, 200000),
    "Mobile Banking": _vv("payments.channel.mobile", "Mobile Banking App Transactions", 10000, 500000),
    "Call Centers": _vv("payments.channel.ivr", "Call Center/IVR Transactions", 100, 5000),
    "E-Commerce": _vv("payments.channel.ecommerce", "E-Commerce (CNP) Transactions", 5000, 20000),
    "E-Money Wallet": _vv("payments.channel.ewallet", "E-Money Wallet Transactions", 5000, 20000),
    "Branchless/Digital Banking Apps": _vv("payments.channel.bb_app", "Branchless/Digital Banking App Transactions", 20000, 100000),
}

# A-15 Raast Payments (Volume mn, Value PKR bn).
_A15_RAAST: dict[str, tuple[Series, Series]] = {
    "P2P Transfers": _vv("payments.raast.p2p", "Raast P2P Transfers", 5000, 100000),
    "Bulk Payments": _vv("payments.raast.bulk", "Raast Bulk Payments", 5000, 100000),
    "P2M Payments": _vv("payments.raast.p2m", "Raast P2M Payments", 5000, 100000),
    "Total Raast": _vv("payments.raast.total", "Raast Total Transactions", 10000, 200000),
}

ALL_SERIES: list[Series] = (
    [s for _, s in _INFRA]
    + [s for _, s, _ in _PAY] + [s for _, _, s in _PAY]
    + list(_A2_CARDS.values())
    + [s for pair in _A4_CHANNELS.values() for s in pair]
    + [s for pair in _A15_RAAST.values() for s in pair]
)
# de-dup (ewallet appears twice for the two apostrophe glyphs)
_seen: set[str] = set()
ALL_SERIES = [s for s in ALL_SERIES if not (s.id in _seen or _seen.add(s.id))]

_MON = {m: i for i, m in enumerate(calendar.month_abbr) if m}
_NUM = re.compile(r"^(.*?)\s+([\d,]+(?:\.\d+)?)\s+([\d,]+(?:\.\d+)?)\s*$")
_NUM4 = re.compile(r"^(.*?)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s+([\d,]+\.\d+)\s*$")
_END_RE = re.compile(r"End\s+([A-Za-z]{3})-(\d{2})")
_FYQ_RE = re.compile(r"Q(\d)-FY(\d{2})")


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _fy_quarter_to_date(q: int, fy: int) -> date:
    """FY quarter -> calendar quarter-end. FY26 = Jul-2025..Jun-2026;
    Q1=Sep, Q2=Dec (prior calendar year), Q3=Mar, Q4=Jun (fy calendar year)."""
    year = 2000 + fy - 1 if q in (1, 2) else 2000 + fy
    month = {1: 9, 2: 12, 3: 3, 4: 6}[q]
    return _month_end(year, month)


def _is_snapshot(text: str | None) -> bool:
    return bool(text and _END_RE.search(text) and "ATMs" in text
               and "Branchless Banking Agents" in text)


def parse_payment_systems(pages) -> list[Record]:
    """Parse the SNAPSHOT page of a Payment Systems Review PDF. Pure - no net."""
    page = next((p for p in pages if _is_snapshot(p.extract_text())), None)
    if page is None:
        raise ValueError("no SNAPSHOT OF PAYMENT SYSTEMS page found")
    lines = [ln.strip() for ln in page.extract_text().split("\n")]

    records: list[Record] = []

    # --- infrastructure & user counts: two quarter-end columns ---------------
    hdr = next((ln for ln in lines if _END_RE.search(ln)), "")
    ends = _END_RE.findall(hdr)
    if len(ends) >= 2:
        d1 = _month_end(2000 + int(ends[0][1]), _MON[ends[0][0]])
        d2 = _month_end(2000 + int(ends[1][1]), _MON[ends[1][0]])
        for ln in lines:
            m = _NUM.match(ln)
            if not m:
                continue
            for prefix, s in _INFRA:
                if m.group(1).startswith(prefix):
                    records.append(Record(s.id, d1, _num(m.group(2)), {}))
                    records.append(Record(s.id, d2, _num(m.group(3)), {}))
                    break

    # --- payments summary: two quarters x (volume, value) --------------------
    qhdr = next((ln for ln in lines if _FYQ_RE.search(ln)), "")
    qs = _FYQ_RE.findall(qhdr)
    if len(qs) >= 2:
        qd1 = _fy_quarter_to_date(int(qs[0][0]), int(qs[0][1]))
        qd2 = _fy_quarter_to_date(int(qs[1][0]), int(qs[1][1]))
        for ln in lines:
            m = _NUM4.match(ln)
            if not m:
                continue
            for prefix, vol, val in _PAY:
                if m.group(1).startswith(prefix):
                    records.append(Record(vol.id, qd1, _num(m.group(2)), {}))
                    records.append(Record(val.id, qd1, _num(m.group(3)), {}))
                    records.append(Record(vol.id, qd2, _num(m.group(4)), {}))
                    records.append(Record(val.id, qd2, _num(m.group(5)), {}))
                    break

    if not records:
        raise ValueError("SNAPSHOT page found but no rows parsed")
    return records


# ---- annexure parsing (5-quarter history tables) ---------------------------

def _annexure_block(full_text: str, tag: str) -> str:
    """The text block for annexure `tag` (e.g. 'A-15') — the occurrence that is a
    real table (a quarter header follows), not the table-of-contents entry."""
    for m in re.finditer(re.escape(tag) + r"[: ]", full_text):
        seg = full_text[m.start(): m.start() + 3000]
        if _FYQ_RE.search(seg[:220]):
            nxt = re.search(r"\nA-\d+[: ]", seg[5:])
            return seg[: nxt.start() + 5] if nxt else seg
    return ""


def _annexure_quarters(block: str) -> list[date]:
    qline = next((ln for ln in block.split("\n") if len(_FYQ_RE.findall(ln)) >= 3), "")
    return [_fy_quarter_to_date(int(q), int(fy)) for q, fy in _FYQ_RE.findall(qline)]


def _parse_count_annexure(full_text: str, tag: str, rows: dict[str, Series]) -> list[Record]:
    block = _annexure_block(full_text, tag)
    dates = _annexure_quarters(block)
    if not dates:
        return []
    n = len(dates)
    rx = re.compile(r"^(.*?)\s+((?:[\d,]+\s+){%d}[\d,]+)\s*$" % (n - 1))
    out: list[Record] = []
    for ln in block.split("\n"):
        m = rx.match(ln.strip())
        if not m:
            continue
        nums = m.group(2).split()
        if len(nums) != n:
            continue
        for prefix, s in rows.items():
            if m.group(1).startswith(prefix):
                out += [Record(s.id, d, _num(v), {}) for d, v in zip(dates, nums)]
                break
    return out


def _parse_volval_annexure(full_text: str, tag: str,
                           rows: dict[str, tuple[Series, Series]]) -> list[Record]:
    block = _annexure_block(full_text, tag)
    dates = _annexure_quarters(block)
    if not dates:
        return []
    n = len(dates)
    rx = re.compile(r"^(.*?)\s+((?:[\d,]+(?:\.\d+)?\s+){%d}[\d,]+(?:\.\d+)?)\s*$" % (2 * n - 1))
    out: list[Record] = []
    for ln in block.split("\n"):
        m = rx.match(ln.strip())
        if not m:
            continue
        nums = m.group(2).split()
        if len(nums) != 2 * n:
            continue
        for prefix, (vol, val) in rows.items():
            if m.group(1).startswith(prefix):
                for i, d in enumerate(dates):
                    out.append(Record(vol.id, d, _num(nums[2 * i]), {}))
                    out.append(Record(val.id, d, _num(nums[2 * i + 1]), {}))
                break
    return out


def parse_annexures(pages) -> list[Record]:
    """Parse the high-value annexures (cards, per-channel retail, Raast). Pure."""
    full = "\n".join(p.extract_text() or "" for p in pages)
    return (
        _parse_count_annexure(full, "A-2", _A2_CARDS)
        + _parse_volval_annexure(full, "A-4", _A4_CHANNELS)
        + _parse_volval_annexure(full, "A-15", _A15_RAAST)
    )


class SbpPaymentSystemsJob(PdfReportJob):
    name = "sbp_payment_systems"
    source = "SBP"

    def _fy_pages(self) -> list[tuple[int, str]]:
        """(start_year, url) for each fiscal-year subpage, newest first."""
        out: list[tuple[int, str]] = []
        for url in self._links(LANDING, _FY_SLUG):
            m = re.search(_FY_SLUG, url)
            if m and int(m.group(1)) >= _MIN_FY:
                out.append((int(m.group(1)), url))
        return sorted(set(out), reverse=True)

    def _pdf_when(self, url: str) -> date:
        m = re.search(r"Q(\d)FY(\d{2})", url, re.I)
        return _fy_quarter_to_date(int(m.group(1)), int(m.group(2))) if m else date.today()

    def report_urls(self, backfill: bool) -> list[tuple[str, date]]:
        pages = self._fy_pages()
        if not backfill:
            pages = pages[:1]  # newest fiscal year only
        out: list[tuple[str, date]] = []
        for _, page_url in pages:
            for pdf in self._links(page_url, r"\.pdf$"):
                if re.search(r"PS-Review", pdf, re.I):
                    out.append((pdf, self._pdf_when(pdf)))
        return out

    def parse_pdf(self, pages, when: date) -> list[Record]:
        # Snapshot and annexures are parsed independently so an off-format older
        # PDF that has one but not the other still yields what it can.
        records: list[Record] = []
        errors: list[str] = []
        for fn in (parse_payment_systems, parse_annexures):
            try:
                records += fn(pages)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
        if not records:
            raise ValueError("; ".join(errors) or "no payment-systems data parsed")
        return records
