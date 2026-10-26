# backend/scripts/

Operator tools. Python ones run as modules from the backend root (`python -m scripts.<name>`) — inside the api
container on the VPS (`docker compose -f docker-compose.server.yml exec -T api python -m scripts.<name>`), on
GitHub Actions in the public ingestion repo, or on the PC (`docker compose run --rm --no-deps -v
"$PWD/backend:/app" api python scripts/<name>.py`). Destructive ones default to a dry run; pass `--commit`.

| Script | Where | What |
|---|---|---|
| `dq_audit.py` | api container, GHA `dq-verify.yml` | Data-quality audit: `detect`, `reconcile-easydata`, `apply-easydata`, `compare-records`, `mark`, `resolve-copies`, `make-notes`, `apply-fund-navs`, `report`. See `docs/data-quality.md` |
| `dq_fetch_parse.py` | PC | Run a job's own fetch + parse and write records JSON (`job <name> out.json [--backfill]`, `spi out.json`) — for workstation jobs and source checks |
| `load_records.py` | api container | Load a records JSON through the job's validate/upsert + an `ingestion_runs` row (`<job> file.json [--commit]`) |
| `load_trade_hs.py` | PC (`parse`), api container (`load`, `names`) | Trade by product × partner: parse PBS volumes → csv.gz, COPY-load into `trade_hs`, recompute readable names |
| `amis_backfill.py` | PC (`fqp`, `minmax`, `pack`), api container (`load`) | AMIS history backfill (FQP done 2026-10-05; `minmax` deferred — ~100 GB, ask first) |
| `mufap_fetch_bundle.py` | GHA `mufap.yml` | Burst-fetch MUFAP daily pages on a good runner IP (exit 2 = blocked) |
| `mufap_jobs_todo.py` | GHA `mufap.yml` | Which MUFAP jobs are still due this slot (bundle-aware) |
| `ocr_merit_orders.py` | PC (rapidocr venv) | OCR ISMO merit-order scans into tokens JSON (skips done documents) |
| `vintages_core.py` | api container | Revision-vintage engine: `apply` (FULL rebuild of all sources), `candidates`, `seed` |
| `vintages_sbp_msb.py`, `vintages_sbp_msb_tables.py` | PC / api container | SBP Monthly Statistical Bulletin adapters (download, parse) for `vintages_core` |
| `vintage_series_msb.json` | — | Bulletin-only series declared for `vintages_core seed` |
| `refresh_easydata_metadata.py` | GHA `refresh-meta.yml` | Re-fetch EasyData dataset metas into `series_meta_staging` (names/descriptions) |
| `disambiguate_series_names.py` | api container | Make active series names unique after a metadata swap (dry run, then `--commit`) |
| `set_easydata_bounds.py` | api container | Learn validation bounds for EasyData series from their history (idempotent) |
| `apply_easydata_units.py` | api container | Write the SBP-verified dataset scales (`ingestion/easydata_units.py`) to `series.unit`; dry run, `--commit` backs up first |
| `harvest_dataset_codes.py` | PC (Playwright) | Rebuild `ingestion/dataset_codes.json` from the EasyData portal tree |
| `check_repo.py` | CI | Consistency checks: every job registered, scheduled or monitored, workflows only run registered jobs |
| `backup.sh`, `backup_loop.sh` | `backup` container | Nightly `pg_dump` + verified scratch restore |

`archive/` holds retired one-offs (kept for reference, not run by anything) — see `archive/README.md`.
Sitemap and IndexNow scripts are in `ops/seo/`.
