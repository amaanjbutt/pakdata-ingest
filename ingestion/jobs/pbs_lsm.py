"""pbs_lsm - PBS Large Scale Manufacturing output (Quantum Index, QIM), monthly.

Source: the "Download QIM Series" workbook linked on PBS's industry page and on every monthly
QIM release. Until the May-2026 release it sat at one fixed URL,
    https://www.pbs.gov.pk/wp-content/uploads/2020/07/Trend-sheet.xlsx
but from June 2026 PBS uploads each month's copy under a new name
(`QIM-Series-Base-Year-2015-16-11.xlsx` for August 2026) and the fixed file stopped at May
2026 — the job read it for three months and stored nothing new. So the job now follows the
link: the industry page's QIM-Series/Trend-sheet workbook, else the newest such upload in
PBS's WordPress media library (`/wp-json/wp/v2/media`), else the old fixed URL.
It is the full monthly history in one sheet:

    Months            QIM      Monthly %Change      Cummulative
                               MoM        YoY       QIM        %Change
    2016-07-15 ...    91.71    -4.64      0.83      91.71      0.83

The date cell is the release/reference date; we key each observation to the
month end of its year+month. We store the QIM index and its MoM / YoY changes
as generic series (cumulative columns are fiscal-YtD and derivable, so skipped).
"""
from __future__ import annotations

import calendar
import io
import json
import re
from datetime import date, datetime

from ingestion.framework import FetchedFile, IngestionJob, Record

QIM_URL = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Trend-sheet.xlsx"
INDUSTRY_PAGE = "https://www.pbs.gov.pk/industry-2/"
MEDIA_API = ("https://www.pbs.gov.pk/wp-json/wp/v2/media?search={q}&per_page=100"
             "&orderby=date&order=desc&_fields=date,source_url")
_QIM_FILE = re.compile(r"(?:QIM[-_ ]?Series|Trend[-_ ]?sheet)[^\"'/<>]*\.xlsx$", re.I)
_QIM_LINK = re.compile(r"""href=["']([^"']+?/(?:QIM[-_ ]?Series|Trend[-_ ]?sheet)[^"'/<>]*\.xlsx)["']""", re.I)

# Column index -> series id (0-based within the sheet row).
_COLUMNS = {1: "lsm.qim", 2: "lsm.qim.mom", 3: "lsm.qim.yoy"}
ALL_SERIES = list(_COLUMNS.values())


def _month_end(d: datetime) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def parse_qim_xlsx(content: bytes) -> list[Record]:
    """Parse the QIM trend-sheet workbook into records. Pure - no DB/network."""
    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(content), data_only=True).active
    records: list[Record] = []
    seen: set[tuple[str, date]] = set()

    for row in ws.iter_rows(values_only=True):
        if not row or not isinstance(row[0], datetime):
            continue
        obs_date = _month_end(row[0])
        for col, sid in _COLUMNS.items():
            if col >= len(row):
                continue
            val = _num(row[col])
            if val is None:
                continue
            key = (sid, obs_date)
            if key in seen:
                continue
            seen.add(key)
            records.append(Record(sid, obs_date, val, {}))

    if not records:
        raise ValueError("no QIM rows parsed from trend sheet")
    return records


def qim_link(page_html: str) -> str | None:
    """PBS page HTML → the absolute URL of the QIM-Series / Trend-sheet workbook it links. Pure."""
    m = _QIM_LINK.search(page_html)
    if not m:
        return None
    url = m.group(1)
    return url if url.startswith("http") else "https://www.pbs.gov.pk/" + url.lstrip("/")


def newest_qim_upload(media_json: str) -> str | None:
    """PBS WordPress media-library JSON → the newest QIM-Series / Trend-sheet .xlsx upload. Pure."""
    try:
        items = json.loads(media_json)
    except ValueError:
        return None
    files = [(x.get("date") or "", x.get("source_url") or "") for x in items if isinstance(x, dict)]
    files = [(d, u) for d, u in files if _QIM_FILE.search(u.rsplit("/", 1)[-1])]
    return max(files)[1] if files else None


class PbsLsmJob(IngestionJob):
    name = "pbs_lsm"
    source = "PBS"

    def _current_url(self) -> str:
        """The workbook PBS links as current: industry page → media library → fixed URL."""
        try:
            url = qim_link(self.http_get(INDUSTRY_PAGE).decode("utf-8", "replace"))
            if url:
                return url
        except Exception:  # noqa: BLE001 — fall through to the media library
            pass
        for q in ("QIM-Series", "Trend-sheet"):
            try:
                url = newest_qim_upload(self.http_get(MEDIA_API.format(q=q)).decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                url = None
            if url:
                return url
        return QIM_URL

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # One workbook holds the full history; incremental == backfill (upserts
        # keep it idempotent).
        content = self.http_get(self._current_url())
        return [FetchedFile(filename="qim_trend.xlsx", content=content,
                            when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        return parse_qim_xlsx(f.content)
