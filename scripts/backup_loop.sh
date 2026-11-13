#!/bin/sh
# Daily backup loop for the `backup` compose service: runs backup.sh once a day at
# BACKUP_AT (UTC, HH:MM, default 20:00 = 01:00 PKT, a quiet hour for the API and the
# ingestion runners). A fixed hour (not "sleep 24h after the last run") keeps the dump
# from drifting into ingestion windows and means a deploy that recreates this
# container doesn't start a dump + full restore test mid-day: on start it backs up at
# once only when the last good backup is older than 26 h. A failed backup is logged
# (and the scheduler alerts on a stale /backups/last_ok); it never stops the loop.
set -u
AT=${BACKUP_AT:-20:00}

until pg_isready >/dev/null 2>&1; do
  echo "[backup] waiting for db..."; sleep 5
done

# Age in seconds of the last good backup: the last_ok marker, else the newest dump file.
last_good_age() {
  now=$(date -u +%s)
  if [ -s /backups/last_ok ]; then
    t=$(cut -d' ' -f1 /backups/last_ok)
  else
    newest=$(ls -1t /backups/pakdata_*.dump 2>/dev/null | head -1)
    t=$([ -n "$newest" ] && stat -c %Y "$newest" || echo 0)
  fi
  echo $(( now - ${t:-0} ))
}

if [ "$(last_good_age)" -gt 93600 ]; then
  echo "[backup] last good backup is over 26 h old — backing up now"
  /scripts/backup.sh || echo "[backup] backup run failed (continuing)"
fi

while true; do
  now=$(date -u +%s)
  next=$(date -u -d "today $AT" +%s)
  [ "$next" -gt "$now" ] || next=$(date -u -d "tomorrow $AT" +%s)
  echo "[backup] next backup $(date -u -d "@$next" +%FT%TZ)"
  sleep $(( next - now ))
  /scripts/backup.sh || echo "[backup] backup run failed (continuing)"
done
