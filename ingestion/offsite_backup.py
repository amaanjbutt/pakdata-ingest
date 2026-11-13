"""Off-site copies of the nightly DB dumps, run by the scheduler (not a registered ingestion job).

The `backup` container dumps the DB at 20:00 UTC and proves each dump restorable (backend/scripts/backup.sh).
This job takes the newest verified dump from /backups (read-only here), encrypts it and uploads it to a PRIVATE
GitHub repo as an asset of the release `db-backups`, then rotates the off-site set: the newest dump of each of
the last KEEP_DAILY days, KEEP_WEEKLY ISO weeks and KEEP_MONTHLY months (the same rule as backup_rotate.sh).

Encryption is plain `openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -md sha256`, so a restore needs only
openssl (restore steps: the repo's README and docs/operations.md → Backups). The dump is streamed through openssl
into the upload — no temporary copy on disk. The dumps hold user and key data: never upload them unencrypted.

Environment (scheduler only): BACKUP_OFFSITE_REPO (docker-compose.server.yml), BACKUP_OFFSITE_KEY (the
passphrase — VPS .env.scheduler + secrets.md; without it the files are useless), and BACKUP_OFFSITE_TOKEN or,
failing that, REQLOG_ARCHIVE_TOKEN (GitHub write access to the repo). Unconfigured → does nothing.
Success is recorded in Redis (`pakdata:offsite:last_ok`), which the hourly resource_check watches.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from collections.abc import Iterable, Iterator
from datetime import date

log = logging.getLogger(__name__)

TAG = "db-backups"
OPENSSL_ENC = ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000", "-md", "sha256",
               "-pass", "env:BACKUP_OFFSITE_KEY"]
REDIS_KEY = "pakdata:offsite:last_ok"
_DUMP = re.compile(r"^pakdata_(\d{8})_\d{6}\.dump$")
_ASSET = re.compile(r"^pakdata_(\d{8})_\d{6}\.dump\.enc$")


def _cfg() -> dict:
    return {
        "repo": os.getenv("BACKUP_OFFSITE_REPO", ""),
        "token": os.getenv("BACKUP_OFFSITE_TOKEN") or os.getenv("REQLOG_ARCHIVE_TOKEN", ""),
        "key": os.getenv("BACKUP_OFFSITE_KEY", ""),
        "dir": os.getenv("BACKUP_DIR", "/backups"),
        "daily": int(os.getenv("BACKUP_OFFSITE_KEEP_DAILY", "3")),
        "weekly": int(os.getenv("BACKUP_OFFSITE_KEEP_WEEKLY", "4")),
        "monthly": int(os.getenv("BACKUP_OFFSITE_KEEP_MONTHLY", "6")),
    }


def configured() -> bool:
    c = _cfg()
    return bool(c["repo"] and c["token"] and c["key"])


def encrypted_size(n: int) -> int:
    """Size of `openssl enc -salt` output for n input bytes: "Salted__" + 8-byte salt + PKCS#7-padded blocks."""
    return 16 + (n // 16 + 1) * 16


def keep(names: Iterable[str], daily: int, weekly: int, monthly: int) -> set[str]:
    """The asset names to keep: newest per day / ISO week / month, counted over the periods that have one
    (borg/restic semantics, same as backend/scripts/backup_rotate.sh). Names that aren't dumps are ignored."""
    kept: set[str] = set()
    seen: dict[str, set] = {"d": set(), "w": set(), "m": set()}
    count = {"d": 0, "w": 0, "m": 0}
    limit = {"d": max(daily, 1), "w": weekly, "m": monthly}
    for name in sorted((n for n in names if _ASSET.match(n)), reverse=True):   # newest first
        d = date(int(name[8:12]), int(name[12:14]), int(name[14:16]))
        for k, bucket in (("d", d), ("w", d.isocalendar()[:2]), ("m", (d.year, d.month))):
            if bucket not in seen[k]:
                seen[k].add(bucket)
                if count[k] < limit[k]:
                    count[k] += 1
                    kept.add(name)
    return kept


def latest_dump(backup_dir: str) -> str | None:
    """Path of the newest verified dump: the one `last_ok` names, else the newest dump file."""
    try:
        with open(os.path.join(backup_dir, "last_ok")) as fh:
            name = fh.read().split()[2]
        if _DUMP.match(name) and os.path.isfile(os.path.join(backup_dir, name)):
            return os.path.join(backup_dir, name)
    except (OSError, IndexError):
        pass
    dumps = sorted(f for f in os.listdir(backup_dir) if _DUMP.match(f)) if os.path.isdir(backup_dir) else []
    return os.path.join(backup_dir, dumps[-1]) if dumps else None


def encrypt_stream(path: str, digest, chunk: int = 1 << 20) -> Iterator[bytes]:
    """Yield `openssl enc` output for `path` (passphrase from BACKUP_OFFSITE_KEY), feeding `digest` as it goes.
    Raises if openssl fails — after the last chunk, so a broken stream never reads as a finished upload."""
    proc = subprocess.Popen([*OPENSSL_ENC, "-in", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert proc.stdout is not None
        while block := proc.stdout.read(chunk):
            digest.update(block)
            yield block
    finally:
        if proc.stdout:
            proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if proc.wait() != 0:
            raise RuntimeError(f"openssl enc failed: {err.strip()[:300]}")


class GitHubBackupStore:
    """All dumps as assets of one release (`db-backups`) in a private repo."""

    api = "https://api.github.com"

    def __init__(self, repo: str, token: str):
        import httpx
        self.repo = repo
        self.http = httpx.Client(timeout=httpx.Timeout(60.0, read=600.0, write=600.0), headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    def release(self) -> dict:
        r = self.http.get(f"{self.api}/repos/{self.repo}/releases/tags/{TAG}")
        if r.status_code == 404:
            r = self.http.post(f"{self.api}/repos/{self.repo}/releases", json={
                "tag_name": TAG, "name": "Encrypted database dumps",
                "body": "Off-site copies of the nightly, restore-tested PakDataHub dumps (AES-256, see README)."})
        r.raise_for_status()
        return r.json()

    def assets(self, rel: dict) -> list[dict]:
        out, page = [], 1
        while True:
            r = self.http.get(f"{self.api}/repos/{self.repo}/releases/{rel['id']}/assets",
                              params={"per_page": 100, "page": page})
            r.raise_for_status()
            out += r.json()
            if len(r.json()) < 100:
                return out
            page += 1

    def upload(self, rel: dict, name: str, chunks: Iterator[bytes], size: int) -> dict:
        up = rel["upload_url"].split("{")[0]
        r = self.http.post(up, params={"name": name}, content=chunks, headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(size)})
        r.raise_for_status()
        return r.json()

    def set_label(self, asset_id: int, label: str) -> None:
        self.http.patch(f"{self.api}/repos/{self.repo}/releases/assets/{asset_id}",
                        json={"label": label}).raise_for_status()

    def delete(self, asset_id: int) -> None:
        self.http.delete(f"{self.api}/repos/{self.repo}/releases/assets/{asset_id}").raise_for_status()


def run(store=None, redis_client=None) -> dict:
    """Upload the newest verified dump (unless already there), then rotate. Safe to re-run."""
    c = _cfg()
    if not (c["repo"] and c["token"] and c["key"]):
        log.info("offsite_backup: not configured (BACKUP_OFFSITE_REPO / _KEY / token)")
        return {"status": "unconfigured"}
    dump = latest_dump(c["dir"])
    if dump is None:
        raise RuntimeError(f"no dump in {c['dir']}")
    store = store or GitHubBackupStore(c["repo"], c["token"])
    rel = store.release()
    assets = store.assets(rel)

    name = os.path.basename(dump) + ".enc"
    size = encrypted_size(os.path.getsize(dump))
    have = next((a for a in assets if a["name"] == name), None)
    uploaded = False
    if not (have and have.get("size") == size and have.get("state") == "uploaded"):
        if have:  # a broken earlier attempt
            store.delete(have["id"])
        t0, digest = time.monotonic(), hashlib.sha256()
        asset = store.upload(rel, name, encrypt_stream(dump, digest), size)
        if asset.get("size") != size or asset.get("state") != "uploaded":
            raise RuntimeError(f"upload of {name} not confirmed: size {asset.get('size')} != {size} "
                               f"or state {asset.get('state')}")
        store.set_label(asset["id"], f"sha256 {digest.hexdigest()}")
        log.info("offsite_backup: uploaded %s (%.0f MB) in %.0f s", name, size / 2**20, time.monotonic() - t0)
        uploaded = True
        assets = store.assets(rel)

    kept = keep([a["name"] for a in assets], c["daily"], c["weekly"], c["monthly"])
    kept.add(name)  # never rotate out what was just verified, whatever its date
    removed = []
    for a in assets:
        if _ASSET.match(a["name"]) and a["name"] not in kept:
            store.delete(a["id"])
            removed.append(a["name"])
    if redis_client is not None:
        redis_client.set(REDIS_KEY, f"{int(time.time())} {name}")
    log.info("offsite_backup: %s %s; %d kept off-site, rotated out %s", "uploaded" if uploaded else "already had",
             name, len(kept), removed or "nothing")
    return {"status": "ok", "asset": name, "uploaded": uploaded, "kept": sorted(kept), "removed": removed}
