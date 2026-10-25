"""Offline unit tests for webhook signing and plan limits."""
from __future__ import annotations

from app.api.webhooks import WEBHOOK_LIMITS
from app.services.webhook_delivery import sign


def test_sign_is_deterministic_hmac_sha256():
    a = sign(b'{"x":1}', "s3cret")
    b = sign(b'{"x":1}', "s3cret")
    assert a == b
    assert len(a) == 64  # sha256 hex
    assert sign(b'{"x":2}', "s3cret") != a  # body change flips the signature
    assert sign(b'{"x":1}', "other") != a   # secret change flips the signature


def test_webhook_limits_match_pricing():
    assert WEBHOOK_LIMITS["free"] == 0
    assert WEBHOOK_LIMITS["developer"] == 3
    assert WEBHOOK_LIMITS["pro"] == 50
    assert WEBHOOK_LIMITS["business"] is None
