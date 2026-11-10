# pakdata-ingest (mirror)

This public repository runs PakDataHub's scheduled ingestion on GitHub Actions — the jobs whose publishers
(SBP, SBP EasyData, MUFAP, PBS) block the production server. Each workflow opens a restricted SSH tunnel to the
production database and runs `python -m ingestion.run <job>`.

**Do not edit this repository directly.** It is overwritten on every merge to the private source repository
(`backend/` → repo root, `ops/gha-ingest/workflows/` → `.github/workflows/`). Make changes there.

No secrets are stored in this repository; workflows read them from GitHub Actions secrets.

Website: https://pakdatahub.com · API: https://api.pakdatahub.com
