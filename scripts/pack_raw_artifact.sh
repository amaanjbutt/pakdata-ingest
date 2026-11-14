#!/usr/bin/env bash
# pack_raw_artifact.sh — at the end of a GitHub Actions ingestion job, pack the publisher files the run
# downloaded (RAW_ARCHIVE_DIR, default /tmp/raw) for the private raw-file archive, ENCRYPTED: artifacts
# of this public repo can be downloaded by anyone signed in to GitHub. Only the VPS holds the private key;
# its weekly job (ops/archive/archive_release.py vps-topup in the private repo) downloads, decrypts and
# archives them. No secret is needed here: the key below is a public key.
#
#   bash scripts/pack_raw_artifact.sh [/tmp/raw] [/tmp/raw-artifact]
#
# Writes <out>/raw.tgz.enc (tar.gz, AES-256-CBC with a random one-off key, PBKDF2) and <out>/key.enc
# (that key, RSA-OAEP/SHA-256 to the public key below). No files: writes nothing, exits 0.
# RAW_ARCHIVE_PUBKEY=<pem file> overrides the key (tests).
set -euo pipefail
src=${1:-${RAW_ARCHIVE_DIR:-/tmp/raw}}
out=${2:-/tmp/raw-artifact}
if [ ! -d "$src" ] || [ -z "$(find "$src" -type f -print -quit)" ]; then
  echo "no downloaded files in $src — nothing to keep"; exit 0
fi
mkdir -p "$out"
pub=${RAW_ARCHIVE_PUBKEY:-}
if [ -z "$pub" ]; then
  # the archive's public key (RSA-4096, 2026-11-15); the private half is only on the VPS and in secrets.md
  pub=$(mktemp)
  cat > "$pub" <<'PUBKEY'
-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA9MPd+Y1iP+a+cpaDBI0t
l6B8MPsJi4ApXQxWLeedF9M7WrVsUUaBaWinNgRWTR550BEe58n+0IVViDQL+PNX
18DSOW7lcFog3OS3X44KJ06TJS17umxfw3vfbHp1baz0uNosn2MIq94c4VQx4fiI
V6aBhFehv5vtfg2z3dS7GKQk3NOeeKAKkFlHtTIalq8hzoAe3lsT4byQe0xLgZ6S
QqEfFm3ooCiR3fC0JP4lDkA62u/H+x7f+Rz6GmuNEUhXmRJZXeO6Tdwe/2ZGwY6V
xrobJqZLRAx5TIUGz/IyfNxbB9iNUX/JwcezvnMS7UZ379EYx64tlevNdl7iIfZ6
wA9e9BWiO4kgGi7buPjdRYE/h6x20HzBS9s3utmPi2d74jrZvSIbkGv3dOXn/L3f
2HGP5zymo4Z3sUEUrcwAz/YSteI/hGEEl8urcyZojiUdsEqlLMm6vFdh7zLiTCTu
uVEB+NKH/EX5pNGPUTNUvQXK9H9XKGerIDT/lPAP8qQJHNeJS5IUHwDAC+JychRd
8NOp/fGEFELE22N8Q/RA+ZhqqpZTUYc//zN/cfNdiB+8yXIWd1fOmszpIoBp+y6M
eLKtXQcrf5JnRXZhTcHVWeMRpYRjRVy8c3diVBbznSjH1PrEpOc0OwHMv3D+LZz/
DR8I93mpnqax74dIRb3WAmMCAwEAAQ==
-----END PUBLIC KEY-----
PUBKEY
fi
RAW_ARCHIVE_PASS=$(openssl rand -hex 32)
export RAW_ARCHIVE_PASS
tar -C "$src" -czf - . \
  | openssl enc -aes-256-cbc -pbkdf2 -iter 100000 -salt -pass env:RAW_ARCHIVE_PASS -out "$out/raw.tgz.enc"
printf '%s' "$RAW_ARCHIVE_PASS" | openssl pkeyutl -encrypt -pubin -inkey "$pub" \
  -pkeyopt rsa_padding_mode:oaep -pkeyopt rsa_oaep_md:sha256 -out "$out/key.enc"
unset RAW_ARCHIVE_PASS
[ -n "${RAW_ARCHIVE_PUBKEY:-}" ] || rm -f "$pub"
echo "kept $(find "$src" -type f | wc -l) files ($(du -sh "$src" | cut -f1)) as an encrypted artifact"
