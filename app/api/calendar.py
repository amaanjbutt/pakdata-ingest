from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query

from app import db

router = APIRouter(prefix="/v1", tags=["calendar"])


@router.get("/calendar")
def calendar(
    next: int | None = Query(None, ge=1, le=100, description="Return the next N upcoming releases"),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    module: str | None = Query(None),
):
    """Public release calendar — when each indicator is next expected to publish.

    `?next=N` returns the next N upcoming releases; otherwise a `from`/`to` window
    (defaulting to the next 30 days). No key needed.
    """
    clauses = ["1=1"]
    params: dict = {}
    if next:
        clauses.append("c.release_date >= CURRENT_DATE")
        params["limit"] = next
        limit_sql = "LIMIT %(limit)s"
    else:
        clauses.append("c.release_date >= COALESCE(%(from)s, CURRENT_DATE)")
        clauses.append("c.release_date <= COALESCE(%(to)s, CURRENT_DATE + 30)")
        params["from"] = date_from
        params["to"] = date_to
        limit_sql = ""
    if module:
        clauses.append("s.module = %(module)s")
        params["module"] = module

    rows = db.query(
        f"""
        SELECT c.release_date AS date, c.indicator, c.source, c.series_id, c.actual_value
        FROM release_calendar c
        LEFT JOIN series s ON s.id = c.series_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.release_date, c.indicator
        {limit_sql}
        """,
        params,
    )
    return {"success": True, "data": rows, "count": len(rows)}


def _ics_text(s: str) -> str:
    """RFC 5545 TEXT escaping: backslash, semicolon, comma, newline."""
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@router.get("/calendar.ics")
def calendar_ics(module: str | None = Query(None)):
    """The release calendar as an iCalendar feed (public): subscribe to it in Google
    Calendar, Outlook or Apple Calendar. All-day events, the last 30 days and the coming
    ~4 months, one per release; each links to its series."""
    import hashlib
    from datetime import datetime, timezone

    from fastapi.responses import Response

    params: dict = {"module": module}
    rows = db.query(
        """SELECT c.release_date AS date, c.indicator, c.source, c.series_id
           FROM release_calendar c LEFT JOIN series s ON s.id = c.series_id
           WHERE c.release_date >= CURRENT_DATE - 30
             AND (%(module)s::text IS NULL OR s.module = %(module)s)
           ORDER BY c.release_date, c.indicator""", params)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//PakDataHub//Release calendar//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "X-WR-CALNAME:Pakistan data releases (PakDataHub)",
             "X-WR-CALDESC:When Pakistan's economic indicators publish next. Dates are best estimates from each "
             "release's usual cadence.", "REFRESH-INTERVAL;VALUE=DURATION:PT12H"]
    for r in rows:
        d = r["date"]
        nxt = date.fromordinal(d.toordinal() + 1)
        sid = r["series_id"] or ""
        url = f"https://pakdatahub.com/series/{sid}" if sid else "https://pakdatahub.com/calendar"
        # stable across requests and processes, so a subscribed calendar updates events in place
        uid = f"{d:%Y%m%d}-{hashlib.md5(r['indicator'].encode()).hexdigest()[:12]}@pakdatahub.com"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{d:%Y%m%d}",
                  f"DTEND;VALUE=DATE:{nxt:%Y%m%d}", f"SUMMARY:{_ics_text(r['indicator'] + ' (' + r['source'] + ')')}",
                  f"DESCRIPTION:{_ics_text('Expected release. Series: ' + (sid or 'see the calendar') + chr(10) + url)}",
                  f"URL:{url}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'inline; filename="pakdatahub-releases.ics"',
                             "Cache-Control": "public, max-age=3600"})
