"""Transactional email via Resend.

Best-effort: if RESEND_API_KEY is unset or the send fails, we log and move on —
email delivery must never break signup/checkout. Uses the same httpx dependency
already vendored for billing.
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings

log = logging.getLogger("pakdata.email")

_API = "https://api.resend.com/emails"


def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email. Returns True on a 2xx from Resend, False otherwise
    (including when no API key is configured). Never raises."""
    if not settings.resend_api_key:
        return False
    try:
        r = httpx.post(
            _API,
            headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code // 100 == 2:
            return True
        log.warning("email send failed (%s): %s", r.status_code, r.text[:200])
        return False
    except Exception as exc:  # noqa: BLE001 — email is best-effort
        log.warning("email send error: %s", exc)
        return False


def _shell(title: str, body_html: str) -> str:
    return (
        f'<div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:560px;'
        f'margin:0 auto;color:#1a1a1a;line-height:1.55">'
        f'<h1 style="font-size:20px;margin:0 0 16px">{title}</h1>{body_html}'
        f'<hr style="border:none;border-top:1px solid #e5e5e5;margin:28px 0 14px">'
        f'<p style="font-size:12px;color:#888">PakDataHub · '
        f'<a href="{settings.public_base_url}" style="color:#14503c">pakdatahub.com</a></p></div>'
    )


def send_api_key_email(to: str, api_key: str, *, plan: str = "free") -> bool:
    """Deliver a newly-issued API key with a short getting-started nudge."""
    body = (
        f'<p>Welcome to PakDataHub — your <b>{plan}</b> account is ready.</p>'
        f'<p>Here is your API key. Keep it secret; it is shown only once.</p>'
        f'<pre style="background:#f5f5f3;border:1px solid #e5e5e5;padding:12px 14px;'
        f'border-radius:6px;font-size:13px;overflow-x:auto">{api_key}</pre>'
        f'<p style="margin-top:18px"><a href="{settings.public_base_url}/docs/getting-started" '
        f'style="background:#14503c;color:#fff;text-decoration:none;padding:10px 18px;'
        f'border-radius:6px;display:inline-block">Make your first call</a></p>'
        f'<p style="font-size:13px;color:#666;margin-top:16px">Manage keys, usage, and billing '
        f'in your <a href="{settings.public_base_url}/dashboard" style="color:#14503c">dashboard</a>.</p>'
    )
    return send_email(to, "Your PakDataHub API key", _shell("Your API key is ready", body))


def admin_recipients() -> list[str]:
    return [e.strip().lower() for e in (settings.admin_emails or "").split(",") if e.strip()]


def send_admin_notification(subject: str, body_html: str) -> None:
    """Notify the site admin(s) of a noteworthy event (new signup, subscription).
    Best-effort — send_email never raises, so this can't break signup/checkout."""
    for to in admin_recipients():
        send_email(to, f"[PakDataHub] {subject}", _shell(subject, body_html))


def send_welcome_email(to: str) -> bool:
    """Welcome a new Google sign-up (no key issued — they create one in-app)."""
    body = (
        f'<p>Welcome to PakDataHub. Your free account is ready.</p>'
        f'<p>Create your first API key in the dashboard, then start pulling Pakistani '
        f'financial &amp; economic data.</p>'
        f'<p style="margin-top:18px"><a href="{settings.public_base_url}/dashboard" '
        f'style="background:#14503c;color:#fff;text-decoration:none;padding:10px 18px;'
        f'border-radius:6px;display:inline-block">Open your dashboard</a></p>'
    )
    return send_email(to, "Welcome to PakDataHub", _shell("Welcome to PakDataHub", body))
