"""Minimal Cloudflare R2 (S3 API) client: put an object, create a bucket, presign a GET.

Stdlib + httpx only (AWS Signature Version 4, region "auto"), so the API image needs no boto3.
Credentials come from R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (secrets.md); the
bucket from R2_BULK_BUCKET.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import httpx

from app.config import settings

REGION = "auto"
SERVICE = "s3"


def configured() -> bool:
    return bool(settings.r2_endpoint and settings.r2_access_key_id and settings.r2_secret_access_key)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(day: str) -> bytes:
    k = _hmac(("AWS4" + settings.r2_secret_access_key).encode(), day)
    k = _hmac(k, REGION)
    k = _hmac(k, SERVICE)
    return _hmac(k, "aws4_request")


def _host() -> str:
    return urlsplit(settings.r2_endpoint).netloc


def _path(bucket: str, key: str | None = None) -> str:
    p = "/" + quote(bucket, safe="")
    if key:
        p += "/" + quote(key, safe="/-_.~")
    return p


def _canonical_query(params: dict[str, str]) -> str:
    return "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))


def _signed_headers(method: str, path: str, body_hash: str, extra: dict[str, str] | None = None,
                    query: dict[str, str] | None = None) -> dict[str, str]:
    now = datetime.now(tz=timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    headers = {"host": _host(), "x-amz-content-sha256": body_hash, "x-amz-date": amz_date}
    for k, v in (extra or {}).items():
        headers[k.lower()] = v
    names = sorted(headers)
    canonical = "\n".join([
        method, path, _canonical_query(query or {}),
        "".join(f"{n}:{headers[n].strip()}\n" for n in names),
        ";".join(names), body_hash,
    ])
    scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={settings.r2_access_key_id}/{scope}, "
                                f"SignedHeaders={';'.join(names)}, Signature={sig}")
    return headers


def create_bucket(bucket: str | None = None) -> int:
    """PUT the bucket; 200 = created, 409 = it exists already (both fine)."""
    bucket = bucket or settings.r2_bucket
    path = _path(bucket)
    empty = hashlib.sha256(b"").hexdigest()
    r = httpx.put(f"{settings.r2_endpoint}{path}", headers=_signed_headers("PUT", path, empty), timeout=60)
    if r.status_code not in (200, 409):
        raise RuntimeError(f"R2 create bucket {bucket}: HTTP {r.status_code} {r.text[:300]}")
    return r.status_code


def put_object(key: str, body: bytes, content_type: str, *, disposition: str | None = None,
               bucket: str | None = None) -> None:
    bucket = bucket or settings.r2_bucket
    path = _path(bucket, key)
    extra = {"content-type": content_type}
    if disposition:
        extra["content-disposition"] = disposition
    headers = _signed_headers("PUT", path, hashlib.sha256(body).hexdigest(), extra)
    r = httpx.put(f"{settings.r2_endpoint}{path}", content=body, headers=headers, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(f"R2 put {key}: HTTP {r.status_code} {r.text[:300]}")


def presign_get(key: str, expires: int = 600, *, filename: str | None = None,
                bucket: str | None = None, now: datetime | None = None) -> str:
    """A GET link valid for `expires` seconds (query-string SigV4). `filename` asks the browser to
    save the file under that name."""
    bucket = bucket or settings.r2_bucket
    now = now or datetime.now(tz=timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    scope = f"{day}/{REGION}/{SERVICE}/aws4_request"
    path = _path(bucket, key)
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{settings.r2_access_key_id}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if filename:
        query["response-content-disposition"] = f'attachment; filename="{filename}"'
    canonical = "\n".join(["GET", path, _canonical_query(query), f"host:{_host()}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    sig = hmac.new(_signing_key(day), to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{settings.r2_endpoint}{path}?{_canonical_query(query)}&X-Amz-Signature={sig}"
