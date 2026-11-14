"""Failure / anomaly alerting (PRD §5.4): log + optional webhook + optional email.

Set ALERT_WEBHOOK_URL (Slack/Telegram-compatible incoming webhook) and/or
ALERT_EMAIL_TO (delivered via Resend, see app.services.email) to enable delivery;
otherwise alerts are logged only. Delivery never raises into ingestion.

Where Redis is configured (the VPS api + scheduler), each alert is also tallied by title in an
hourly hash, so the daily health email (scripts/edge_health.py) can say how many were sent.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("pakdata.alert")

_WEBHOOK = os.getenv("ALERT_WEBHOOK_URL")
_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")


def alert_unless_403(title: str, message: str) -> None:
    """Like alert(), but SUPPRESS delivery for a transient Cloudflare 403.

    MUFAP sits behind Cloudflare and 403s most egress IPs. Those failures self-heal —
    mufap.yml (GitHub Actions) retries on a fresh runner IP several times per slot, and
    the hourly staleness_check alerts if a job ever fails to *recover*. Without this
    guard every blocked attempt would email → dozens of alert emails for one outage.
    So a 403 is only logged here; every other failure alerts immediately.
    """
    if "403" in message:
        log.warning("ALERT (403 — email suppressed, auto-heals): %s — %s", title, message)
        return
    alert(title, message)


COUNT_KEY = "pakdata:alerts:{hour}"  # hour = %Y%m%d%H, UTC
_COUNT_TTL = 3 * 86400


def _tally(title: str) -> None:
    """Count the alert for the daily health email. Only where REDIS_URL is set (not on the GitHub
    runners); a second's timeout and never raises."""
    url = os.getenv("REDIS_URL")
    if not url:
        return
    try:
        import redis

        r = redis.from_url(url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True)
        key = COUNT_KEY.format(hour=datetime.now(timezone.utc).strftime("%Y%m%d%H"))
        r.hincrby(key, title[:200], 1)
        r.expire(key, _COUNT_TTL)
    except Exception as exc:  # noqa: BLE001 — counting must never break alerting
        log.debug("alert tally failed: %s", exc)


def alert(title: str, message: str) -> None:
    log.warning("ALERT: %s — %s", title, message)
    _tally(title)
    if _WEBHOOK:
        try:
            httpx.post(_WEBHOOK, json={"text": f"*{title}*\n{message}"}, timeout=10)
        except Exception as exc:  # never let alerting break ingestion
            log.error("alert webhook delivery failed: %s", exc)
    if _EMAIL_TO:
        try:
            from app.services.email import send_email
            body = (
                f'<p style="font-family:system-ui,Arial">PakDataHub ingestion alert.</p>'
                f'<p><b>{title}</b></p>'
                f'<pre style="background:#f5f5f3;padding:12px;border-radius:6px;'
                f'white-space:pre-wrap">{message}</pre>'
            )
            send_email(_EMAIL_TO, f"[PakData alert] {title}", body)
        except Exception as exc:  # never let alerting break ingestion
            log.error("alert email delivery failed: %s", exc)
