"""Failure / anomaly alerting (PRD §5.4). MVP: log + optional webhook.

Set ALERT_WEBHOOK_URL (Slack/Telegram-compatible incoming webhook) to enable
delivery; otherwise alerts are logged only.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("pakdata.alert")

_WEBHOOK = os.getenv("ALERT_WEBHOOK_URL")


def alert(title: str, message: str) -> None:
    log.warning("ALERT: %s — %s", title, message)
    if not _WEBHOOK:
        return
    try:
        httpx.post(_WEBHOOK, json={"text": f"*{title}*\n{message}"}, timeout=10)
    except Exception as exc:  # never let alerting break ingestion
        log.error("alert delivery failed: %s", exc)
