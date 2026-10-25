# Retired one-off scripts

Kept for reference only — nothing runs these. Each did its job once; read one before writing a similar tool.

| Script | What it did | When |
|---|---|---|
| `generate_id_map.py` | Built the legacy → clean `topic.group.item` id map (`ingestion/id_map.json`) | 2026-07-30 |
| `backfill_payouts.py` | Deep MUFAP payout history (the daily job keeps a 45-day window) | 2026-08-17 |
| `backfill_fund_history.py` | Per-fund NAV history from MUFAP's history endpoint | 2026-08-18 |
| `reparse_fund_portfolio.py` + `_driver.sh` | Re-parse `fund_portfolio` after the %-field fix (staging → swap) | 2026-08-17 |
| `probe_portfolio_history.py` + `_driver.sh`, `backfill_portfolio_history.py` + `_driver.sh` | Monthly portfolio history probe + backfill | 2026-08-18 |
| `canon_plant_dims.py` | Rewrote stored plant dims to `plants_pk` canonical names (jobs emit canonical names since) | 2026-10-17 |
| `mufap_403_autoheal.sh` | VPS cron that rotated WARP + re-ran MUFAP jobs on 403 (WARP is dead; MUFAP moved to GHA) | 2026-08-29 → 09-11 |
| `residential_tunnel.cmd` | Reverse SSH tunnel from the PC for SBP/EasyData egress (replaced by GitHub Actions; owner declined PC routing) | 2026-08 |
