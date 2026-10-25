#!/bin/sh
# pg_dump the pakdata DB to /backups (a host bind-mount, so it survives
# `docker compose down -v`), rotate old dumps, then TEST the dump by restoring it
# into a scratch database and verifying row counts. An untested backup is not a
# backup. Exit non-zero on any failure so the caller/alerting can react.
#
# Runs inside a postgres:16 container with PGHOST/PGUSER/PGPASSWORD/PGDATABASE set.
set -eu

KEEP=${BACKUP_KEEP:-14}
DIR=/backups
TS=$(date +%Y%m%d_%H%M%S)
OUT="$DIR/pakdata_$TS.dump"
SCRATCH=pakdata_restoretest
TABLES="observations series fund_navs security_prices auctions trade_flows observation_revisions agri_prices agri_markets agri_commodities debt_trades funds"

echo "[backup $TS] starting"

# Row counts of the live DB BEFORE the dump. pg_dump takes a consistent MVCC
# snapshot, so the restored copy must contain AT LEAST these (concurrent backfill
# only adds rows) — a valid invariant that doesn't false-fail on live writes.
BEFORE=""
for t in $TABLES; do
  c=$(psql -tAc "SELECT count(*) FROM $t;" 2>/dev/null || echo NA)
  BEFORE="$BEFORE $t=$c"
done
echo "[backup $TS] pre-dump counts:$BEFORE"

echo "[backup $TS] dumping -> $OUT"
pg_dump -Fc -f "$OUT"
SIZE=$(wc -c < "$OUT")
echo "[backup $TS] wrote $SIZE bytes"
[ "$SIZE" -gt 1000 ] || { echo "[backup $TS] FAIL: dump implausibly small"; exit 1; }

# Rotate: keep the newest $KEEP dumps.
ls -1t "$DIR"/pakdata_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  echo "[backup $TS] rotating out $f"; rm -f "$f"
done

# ---- restore test -----------------------------------------------------------
echo "[backup $TS] restore-test into $SCRATCH"
psql -c "DROP DATABASE IF EXISTS $SCRATCH;" >/dev/null
psql -c "CREATE DATABASE $SCRATCH;" >/dev/null
# --no-owner/--no-privileges: scratch DB may lack roles; we only verify data.
pg_restore --no-owner --no-privileges -d "$SCRATCH" "$OUT"

fail=0
for t in $TABLES; do
  before=$(echo "$BEFORE" | tr ' ' '\n' | grep "^$t=" | cut -d= -f2)
  rst=$(psql -d "$SCRATCH" -tAc "SELECT count(*) FROM $t;" 2>/dev/null || echo NA)
  echo "[backup $TS] verify $t: before=$before restored=$rst"
  case "$before" in NA|"") continue ;; esac
  if [ "$rst" = "NA" ] || [ "$rst" -lt "$before" ]; then
    echo "[backup $TS] FAIL: $t restored=$rst < pre-dump=$before"; fail=1
  fi
done

psql -c "DROP DATABASE $SCRATCH;" >/dev/null
[ "$fail" -eq 0 ] || { echo "[backup $TS] RESTORE TEST FAILED"; exit 1; }
echo "[backup $TS] OK — dump verified restorable ($OUT)"
