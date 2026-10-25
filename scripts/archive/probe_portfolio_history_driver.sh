#!/bin/bash
# Patient driver for the portfolio-history feasibility probe. The MUFAP POST
# endpoint is walled intermittently; this retries the probe, rotating the WARP IP
# and cooling down between attempts, until a window opens and the probe succeeds
# (PROBE_OK) or the attempt budget is spent. Launch detached on the VPS:
#   nohup bash scripts/probe_portfolio_history_driver.sh >/root/probe_driver.out 2>&1 &
set -u
cd /opt/pakdata
LOG=/root/probe_portfolio_history.log
DC="docker compose -f docker-compose.server.yml"
: > "$LOG"
echo "=== probe driver start $(date) ===" >> "$LOG"
for attempt in $(seq 1 30); do
  echo "--- attempt $attempt $(date) ---" >> "$LOG"
  $DC exec -T -e PYTHONPATH=/app api python scripts/probe_portfolio_history.py >> "$LOG" 2>&1
  if grep -q PROBE_OK "$LOG"; then echo "SUCCEEDED on attempt $attempt" >> "$LOG"; break; fi
  warp-cli --accept-tos disconnect >> "$LOG" 2>&1; sleep 4
  warp-cli --accept-tos connect   >> "$LOG" 2>&1; sleep 6
  sleep 300   # let the Cloudflare rate-challenge decay before the next window try
done
echo "=== probe driver done $(date) ===" >> "$LOG"
