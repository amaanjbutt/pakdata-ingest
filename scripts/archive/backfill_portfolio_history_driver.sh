#!/bin/bash
# Patient driver for the historical monthly portfolio-allocation backfill. The
# gap-filling script aborts a pass (exit 2) on a MUFAP failure streak; here we
# rotate the WARP IP and resume, with a long cooldown when a window is closed,
# until a pass completes (exit 0 = DONE) or the pass budget is spent. Launch
# detached on the VPS:
#   nohup bash scripts/backfill_portfolio_history_driver.sh >/root/pfhist_driver.out 2>&1 &
set -u
cd /opt/pakdata
LOG=/root/backfill_portfolio_history.log
DC="docker compose -f docker-compose.server.yml"
: > "$LOG"
echo "=== portfolio-history backfill start $(date) ===" >> "$LOG"
prev=$($DC exec -T db psql -U pakdata -d pakdata -tAc "SELECT count(*) FROM fund_portfolio" | tr -d '[:space:]')
for pass in $(seq 1 80); do
  echo "--- pass $pass $(date) ---" >> "$LOG"
  $DC exec -T -e PYTHONPATH=/app api python scripts/backfill_portfolio_history.py >> "$LOG" 2>&1
  rc=$?
  now=$($DC exec -T db psql -U pakdata -d pakdata -tAc "SELECT count(*) FROM fund_portfolio" | tr -d '[:space:]')
  gained=$(( ${now:-0} - ${prev:-0} )); prev=${now:-0}
  echo "pass $pass rc=$rc rows=$now gained=$gained" >> "$LOG"
  if [ "$rc" -eq 0 ]; then echo "COMPLETE (full pass, no streak)" >> "$LOG"; break; fi
  warp-cli --accept-tos disconnect >> "$LOG" 2>&1; sleep 4
  warp-cli --accept-tos connect   >> "$LOG" 2>&1; sleep 6
  if [ "${gained:-0}" -le 20 ]; then sleep 300; else sleep 30; fi
done
echo "=== portfolio-history backfill done $(date) rows=$prev ===" >> "$LOG"
