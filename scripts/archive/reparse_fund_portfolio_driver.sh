#!/bin/bash
# Host-side driver for the fund_portfolio staging re-parse (copy-then-swap).
# PATIENT strategy: MUFAP/Cloudflare rate-challenges the whole WARP range under
# load, so hammering rotations doesn't help — only time does. Each pass fills gaps;
# if a pass gains little, we rotate the WARP IP and wait a LONG cooldown so the
# challenge decays; if a pass is productive we press on with a short pause. Runs
# until nearly every active fund is resolved. Launch detached on the VPS:
#   nohup bash scripts/reparse_fund_portfolio_driver.sh >/root/reparse_driver.out 2>&1 &
set -u
cd /opt/pakdata
LOG=/root/reparse_host.log
DC="docker compose -f docker-compose.server.yml"
echo "=== patient driver start $(date) ===" >> "$LOG"
prev=$($DC exec -T db psql -U pakdata -d pakdata -tAc \
  "SELECT count(distinct fund_id) FROM fund_portfolio_staging WHERE asset_class<>'__none__'" | tr -d '[:space:]')
for pass in $(seq 1 300); do
  echo "--- pass $pass $(date) ---" >> "$LOG"
  $DC exec -T -e PYTHONPATH=/app api python scripts/reparse_fund_portfolio.py >> "$LOG" 2>&1
  real=$($DC exec -T db psql -U pakdata -d pakdata -tAc \
    "SELECT count(distinct fund_id) FROM fund_portfolio_staging WHERE asset_class<>'__none__'" | tr -d '[:space:]')
  remaining=$($DC exec -T db psql -U pakdata -d pakdata -tAc \
    "SELECT count(*) FROM funds f WHERE f.is_active AND NOT EXISTS \
     (SELECT 1 FROM fund_portfolio_staging s WHERE s.fund_id=f.fund_id)" | tr -d '[:space:]')
  gained=$(( ${real:-0} - ${prev:-0} )); prev=${real:-0}
  echo "pass $pass real=$real remaining=$remaining gained=$gained" >> "$LOG"
  if [ "${remaining:-99}" -le 4 ]; then echo "CONVERGED remaining=$remaining" >> "$LOG"; break; fi
  # rotate the burned egress IP
  warp-cli --accept-tos disconnect >> "$LOG" 2>&1; sleep 4
  warp-cli --accept-tos connect   >> "$LOG" 2>&1; sleep 6
  # adaptive cooldown: if the pass barely moved, the range is challenged — wait it out
  if [ "${gained:-0}" -le 2 ]; then
    echo "  low gain -> long cooldown 600s $(date)" >> "$LOG"; sleep 600
  else
    sleep 45
  fi
done
echo "=== patient driver done $(date) real=$prev ===" >> "$LOG"
