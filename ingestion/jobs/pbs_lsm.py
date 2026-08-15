"""pbs_lsm - PBS Large Scale Manufacturing output (Quantum Index, QIM), monthly.

Source: the "Download QIM Series" workbook linked on every monthly QIM release,
    https://www.pbs.gov.pk/wp-content/uploads/2020/07/Trend-sheet.xlsx
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
from datetime import date, datetime

from ingestion.framework import FetchedFile, IngestionJob, Record

QIM_URL = "https://www.pbs.gov.pk/wp-content/uploads/2020/07/Trend-sheet.xlsx"

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


class PbsLsmJob(IngestionJob):
    name = "pbs_lsm"
    source = "PBS"

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        # One workbook holds the full history; incremental == backfill (upserts
        # keep it idempotent).
        content = self.http_get(QIM_URL)
        return [FetchedFile(filename="qim_trend.xlsx", content=content,
                            when=date.today())]

    def parse(self, f: FetchedFile) -> list[Record]:
        return parse_qim_xlsx(f.content)
