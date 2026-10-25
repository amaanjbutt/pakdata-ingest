#!/usr/bin/env bash
# Auto-heal the intermittent MUFAP Cloudflare 403 on the WARP egress IP.
#
# WHY THIS EXISTS: MUFAP jobs run inside the Docker container, but `warp-cli`
# (which owns the egress IP) runs on the VPS host — so a job can't rotate its own
# egress. This host-side watchdog closes that gap: run it from cron; if any MUFAP
# job's LATEST run is a 403 failure, it rotates WARP (fresh egress IP) and re-runs
# the failed job(s). One rotation heals them all (shared egress). Once a re-run
# succeeds, the latest run is 'success' and the check no longer fires — self-healing,
# no human action needed.
#
# Install (on the VPS, once):
#   crontab -l 2>/dev/null | grep -q mufap_403_autoheal || \
#   (crontab -l 2>/dev/null; echo '*/20 * * * * /opt/pakdata/scripts/mufap_403_autoheal.sh >> /var/log/mufap_autoheal.log 2>&1') | crontab -
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

cd /opt/pakdata || exit 1
DC="docker compose -f docker-compose.server.yml"
# Every MUFAP job egresses via WARP.
JOBS="mufap_pkrv mufap_debt_prices mufap_fund_navs mufap_fund_returns mufap_fund_stats mufap_tfc_valuations mufap_fund_portfolio mufap_debt_trades"

# Which jobs have a 403 as their most recent run?
failed=""
for job in $JOBS; do
  bad="$($DC exec -T db psql -U pakdata -d pakdata -tA -c \
    "SELECT 1 FROM ingestion_runs r
     WHERE r.job_name='${job}'
       AND r.started_at = (SELECT max(started_at) FROM ingestion_runs WHERE job_name='${job}')
       AND r.status='failed' AND coalesce(r.error,'') LIKE '%403%';" 2>/dev/null | tr -d '[:space:]')"
  [ "$bad" = "1" ] && failed="$failed $job"
done

[ -z "$failed" ] && exit 0   # nothing to heal

echo "$(date -u +%FT%TZ): MUFAP 403 on:$failed -> rotating WARP + re-running"
warp-cli --accept-tos disconnect || true; sleep 3
warp-cli --accept-tos connect    || true; sleep 6
warp-cli --accept-tos status 2>&1 | head -1

for job in $failed; do
  echo "$(date -u +%FT%TZ): re-running $job --force"
  $DC run --rm --no-deps api python -m ingestion.run "$job" --force 2>&1 | tail -2 || true
done
