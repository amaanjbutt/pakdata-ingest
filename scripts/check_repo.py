"""check_repo — consistency checks CI runs on every push (drift used to bite us between copies).

    cd backend && python -m scripts.check_repo

Fails (exit 1) when:
  - a module in ingestion/jobs/ is not imported by ingestion/registry.py (registry imports every job,
    and a missing import silently drops the job everywhere);
  - a registered job has no home: not scheduled on the VPS (SCHEDULES), not monitored as an external
    job (MONITORED_EXTERNAL) and not a workstation job (WORKSTATION);
  - a job in MONITORED_EXTERNAL is not run by any ingestion workflow (ops/gha-ingest/workflows/);
  - a workflow names a job that isn't registered;
  - docs/ingestion.md doesn't mention a registered job (the job catalogue must stay complete);
  - a source file contains control characters (scripted edits once wrote \\b as a backspace into regexes).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
WORKFLOWS = ROOT / "ops" / "gha-ingest" / "workflows"
# Tokens in workflows that look like job names but are helpers.
NOT_JOBS = {"mufap_bundle", "mufap_fetch_bundle", "mufap_jobs_todo", "pta_probe"}
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
SCAN = [("backend", "*.py"), ("backend", "*.sql"), ("frontend/src", "*.ts"), ("frontend/src", "*.tsx"),
        ("frontend/scripts", "*.mjs"), ("frontend/content", "*.md"), ("docs", "*.md"), ("ops", "*.yml"),
        ("ops", "*.sh"), (".github", "*.yml")]


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    from ingestion import schedule
    from ingestion.registry import JOBS
    from scripts.mufap_jobs_todo import DAILY_JOBS

    errors: list[str] = []
    jobs = set(JOBS)

    registry_src = (BACKEND / "ingestion" / "registry.py").read_text(encoding="utf-8")
    for f in sorted((BACKEND / "ingestion" / "jobs").glob("*.py")):
        if f.stem != "__init__" and f"ingestion.jobs.{f.stem} import" not in registry_src:
            errors.append(f"ingestion/jobs/{f.name} is not imported by ingestion/registry.py")

    scheduled = {s.job for s in schedule.SCHEDULES}
    external = {s.job for s in schedule.MONITORED_EXTERNAL}
    for j in sorted(jobs - scheduled - external - set(schedule.WORKSTATION)):
        errors.append(f"job {j!r} has no home: add it to SCHEDULES, MONITORED_EXTERNAL or WORKSTATION")
    for j in sorted((scheduled | external | set(schedule.WORKSTATION)) - jobs):
        errors.append(f"schedule.py names {j!r}, which is not registered")
    both = scheduled & external
    if both:
        errors.append(f"jobs both VPS-scheduled and external (double runs, double quota): {sorted(both)}")

    wf_text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WORKFLOWS.glob("*.yml")))
    prefixes = {j.split("_", 1)[0] for j in jobs}
    tokens = set(re.findall(r"\b(?:%s)_[a-z0-9_]+\b" % "|".join(sorted(prefixes)), wf_text))
    for t in sorted(tokens - jobs - NOT_JOBS):
        errors.append(f"a workflow in ops/gha-ingest/workflows names {t!r}, which is not a registered job")
    run_by_workflows = (tokens & jobs) | set(DAILY_JOBS)
    for j in sorted(external - run_by_workflows):
        errors.append(f"{j!r} is in MONITORED_EXTERNAL but no ingestion workflow runs it")
    for j in sorted(set(DAILY_JOBS) - jobs):
        errors.append(f"scripts/mufap_jobs_todo.DAILY_JOBS names {j!r}, which is not registered")

    catalogue = (ROOT / "docs" / "ingestion.md").read_text(encoding="utf-8")
    for j in sorted(jobs):
        if f"`{j}`" not in catalogue:
            errors.append(f"docs/ingestion.md does not mention job `{j}`")

    for base, pattern in SCAN:
        for f in sorted((ROOT / base).rglob(pattern)):
            if "node_modules" in f.parts or ".output" in f.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            m = CONTROL.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                errors.append(f"{f.relative_to(ROOT)}:{line}: control character {m.group()!r}")

    for e in errors:
        print(f"::error::{e}")
    print(f"check_repo: {len(jobs)} jobs, {len(scheduled)} VPS-scheduled, {len(external)} external, "
          f"{len(schedule.WORKSTATION)} workstation — {len(errors)} problem(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
