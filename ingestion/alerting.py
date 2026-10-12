"""Failure / anomaly alerting (PRD §5.4): log + optional webhook + optional email.

Set ALERT_WEBHOOK_URL (Slack/Telegram-compatible incoming webhook) and/or
ALERT_EMAIL_TO (delivered via Resend, see app.services.email) to enable delivery;
otherwise alerts are logged only. Delivery never raises into ingestion.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("pakdata.alert")

_WEBHOOK = os.getenv("ALERT_WEBHOOK_URL")
_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")


def alert_unless_403(title: str, message: str) -> None:
    """Like alert(), but SUPPRESS delivery for a transient Cloudflare 403.

    MUFAP sits behind Cloudflare and intermittently 403s the WARP egress IP. Those
    failures self-heal — the host-side watchdog scripts/mufap_403_autoheal.sh
    rotates WARP + re-runs within ~20 min, and the hourly staleness_check alerts if
    a job ever fails to *recover*. Without this guard a 403 that persists across the
    20-min retry loop emails once per retry → dozens of alert emails for one outage.
    So a 403 is only logged here; every other failure alerts immediately.
    """
    if "403" in message:
        log.warning("ALERT (403 — email suppressed, auto-heals): %s — %s", title, message)
        return
    alert(title, message)


def alert(title: str, message: str) -> None:
    log.warning("ALERT: %s — %s", title, message)
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
