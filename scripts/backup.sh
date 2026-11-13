#!/bin/sh
# pg_dump the pakdata DB to /backups (a host bind-mount, so it survives
# `docker compose down -v`), TEST the dump by restoring it into a scratch database
# and verifying row counts, and only then rotate old dumps (backup_rotate.sh:
# 7 daily + 4 weekly + 3 monthly by default). An untested backup is not a backup.
#
# On success it writes /backups/last_ok ("<epoch> <utc time> <file> <bytes>"); the
# scheduler's hourly health check alerts when that marker is older than ~30 h, so a
# failing backup is no longer silent. Exit non-zero on any failure.
#
# Runs inside a postgres:16 container with PGHOST/PGUSER/PGPASSWORD/PGDATABASE set.
set -eu

DIR=/backups
HERE=$(dirname "$0")
TS=$(date -u +%Y%m%d_%H%M%S)
OUT="$DIR/pakdata_$TS.dump"
SCRATCH=pakdata_restoretest
TABLES="observations series fund_navs security_prices auctions trade_flows observation_revisions agri_prices agri_markets agri_commodities debt_trades funds users api_keys trade_hs port_vessel_calls customs_tariff_duties"

# The scratch restore is a full copy of the DB on the same disk — never leave it behind.
cleanup() { psql -c "DROP DATABASE IF EXISTS $SCRATCH;" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[backup $TS] starting"

# Room for the dump + a full scratch restore (DB size x 1.3 + 1 GB), checked up front so a
# nearly-full disk fails this run loudly instead of filling the disk under Postgres.
DB_BYTES=$(psql -tAc "SELECT pg_database_size(current_database());")
FREE_KB=$(df -Pk "$DIR" | awk 'NR==2 {print $4}')
NEED_KB=$(( DB_BYTES / 1024 * 13 / 10 + 1048576 ))
echo "[backup $TS] db $((DB_BYTES / 1048576)) MB, free $((FREE_KB / 1024)) MB, need $((NEED_KB / 1024)) MB"
if [ "$FREE_KB" -lt "$NEED_KB" ]; then
  echo "[backup $TS] FAIL: not enough free disk for dump + restore test"; exit 1
fi

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

# Rotate only now: a dump that failed its test never pushes a good one out.
sh "$HERE/backup_rotate.sh" "$DIR" | sed "s/^/[backup $TS] /"

printf '%s %s %s %s\n' "$(date -u +%s)" "$(date -u +%FT%TZ)" "$(basename "$OUT")" "$SIZE" > "$DIR/.last_ok.tmp"
mv "$DIR/.last_ok.tmp" "$DIR/last_ok"
echo "[backup $TS] OK — dump verified restorable ($OUT)"
