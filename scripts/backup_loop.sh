#!/bin/sh
# Daily backup loop for the `backup` compose service. Waits for the DB, takes an
# initial verified backup, then repeats every BACKUP_INTERVAL seconds (default
# 24h). A failed backup is logged but never stops the loop.
set -u
INTERVAL=${BACKUP_INTERVAL:-86400}

# Wait for the database to accept connections before the first dump.
until pg_isready >/dev/null 2>&1; do
  echo "[backup] waiting for db..."; sleep 5
done

while true; do
  /scripts/backup.sh || echo "[backup] backup run failed (continuing)"
  sleep "$INTERVAL"
done
