"""Load pre-parsed records into prod through a job's own validate + upsert.

Heavy backfills (hundreds of PDFs) are fetched and parsed on a workstation — faster than
the 1-vCPU VPS, and the PDFs never touch the server. Only the parsed records travel; this
script runs inside the api container, so the database credentials never leave the box.
It records an `ingestion_runs` row like a normal run (backfill: no webhooks).

  # workstation: parse with the job's own parser into JSON
  #   [{"series_id", "obs_date": "YYYY-MM-DD", "value", "dims": {...}}, ...]
  docker compose -f docker-compose.server.yml cp records.json api:/tmp/records.json
  docker compose -f docker-compose.server.yml exec -T api \\
      python -m scripts.load_records pbs_spi_weekly /tmp/records.json [--commit]
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from ingestion.framework import Record
from ingestion.registry import JOBS


def load(job_name: str, path: str, commit: bool = False) -> None:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    records = [Record(r["series_id"], date.fromisoformat(r["obs_date"]), r["value"], r.get("dims") or {})
               for r in rows]
    dates = sorted({r.obs_date for r in records})
    print(f"{len(records)} records, {len(dates)} dates {dates[0]}..{dates[-1]}, "
          f"{len({r.series_id for r in records})} series")
    print("top series:", Counter(r.series_id for r in records).most_common(3))
    if not commit:
        print("dry run - re-run with --commit to write")
        return
    job = JOBS[job_name]()
    run_id = job._start_run()
    try:
        validated = job.validate(records)
        n = job.upsert(validated)
        rejects = len(getattr(job, "last_rejects", []) or [])
        job._finish(run_id, "success", n, f"loaded from {Path(path).name}; {rejects} rejected" if rejects else None,
                    None)
        print(f"upserted {n} rows ({rejects} rejected by validation)")
    except Exception as exc:
        job._finish(run_id, "failed", 0, str(exc)[:500], None)
        raise


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        sys.exit(1)
    load(args[0], args[1], commit="--commit" in sys.argv)
