"""CLI entrypoint for ingestion jobs.

    python -m ingestion.run sbp_kibor            # run once
    python -m ingestion.run sbp_kibor --backfill # historical mode
    python -m ingestion.run --list
"""
from __future__ import annotations

import argparse
import logging
import sys

from ingestion.registry import JOBS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx logs full request URLs at INFO — which would leak the EasyData api_key
# query param into logs. Quiet it to WARNING (secret hygiene, PRD §Phase 7).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ingestion.run")
    p.add_argument("job", nargs="?", help="job name")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="cap series per run (easydata_sync)")
    p.add_argument("--list", action="store_true", help="list available jobs")
    p.add_argument("--force", action="store_true",
                   help="run even if another ingestion job holds the global lock")
    args = p.parse_args(argv)

    if args.list or not args.job:
        print("Available jobs:")
        for name in sorted(JOBS):
            print(f"  {name}")
        return 0

    job_cls = JOBS.get(args.job)
    if job_cls is None:
        print(f"unknown job: {args.job}", file=sys.stderr)
        return 2

    import inspect

    kwargs = {"backfill": args.backfill}
    if "limit" in inspect.signature(job_cls.run).parameters:
        kwargs["limit"] = args.limit

    # Global single-flight: refuse to start if another ingestion job (scheduled or
    # manual) is already running, so we can't overload the small box. Override with
    # --force for a deliberate exception.
    from ingestion.joblock import GlobalJobLock

    lock = GlobalJobLock(owner=f"cli:{args.job}")
    if not args.force and not lock.try_acquire():
        print(f"another ingestion job is running ({lock.holder()}); "
              f"aborting to avoid overloading the box. Use --force to override.",
              file=sys.stderr)
        return 3
    try:
        result = job_cls().run(**kwargs)
        print(result)
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
