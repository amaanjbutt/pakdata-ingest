"""Monthly SBP Monthly Statistical Bulletin refresh — keeps the bulletin series and the
revision vintages current (HANDOFF §B1.1).

Two jobs, because SBP only answers GitHub's runners while the rebuild belongs next to
the database:

- `sbp_msb_fetch` (GitHub Actions): finds bulletin issues newer than the stored source,
  downloads each one's "Selected Economic Indicators" table, parses it with
  `scripts.vintages_sbp_msb.parse_issue` and appends it to the source JSON.
- `vintages_refresh` (VPS, daily): when the stored sources changed since its last
  success, re-seeds the bulletin series and rebuilds every backfilled vintage
  (`scripts.vintages_core.seed` + `apply` — apply is a full rebuild of ALL sources).

The parsed sources live in the database (`vintage_source_files`), the one place both
sides can reach — never in the public ingestion repo. Load them once with
`python -m ingestion.jobs.sbp_msb_vintages load <msb_2005_2026.json> <msb_na.json>`.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app import db
from ingestion.framework import IngestionJob

IND_SOURCE = "msb_2005_2026.json"  # indicators page of every issue (source sbp_msb)
NA_SOURCE = "msb_na.json"          # national-accounts tables (source sbp_msb_na)
CATCH_UP = 3                       # issues fetched per run at most

_DDL = """CREATE TABLE IF NOT EXISTS vintage_source_files (
    name TEXT PRIMARY KEY, payload JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""


def issue_lag_ok(iss: dict) -> bool:
    """An issue prints its latest 1-3 months; a newest column far from the issue month is
    a mis-filed upload (same guard as vintages_sbp_msb.parse). Pure."""
    y, m = map(int, iss["issue"].split("-"))
    last = max(d for r in iss["rows"] for d in r["values"])
    lag = (y * 12 + m) - (int(last[:4]) * 12 + int(last[5:7]))
    return 0 <= lag <= 6


def new_issues(listed: list[str], have: set[str]) -> list[str]:
    """Issues on SBP's index newer than everything stored, oldest first (at most
    CATCH_UP). Gaps further back are history the backfill already decided on. Pure."""
    newest = max(have) if have else ""
    return sorted(k for k in listed if k not in have and k > newest)[:CATCH_UP]


class SbpMsbFetchJob(IngestionJob):
    name = "sbp_msb_fetch"
    source = "SBP"

    def run(self, backfill: bool = False) -> dict:
        from scripts import vintages_sbp_msb as msb

        run_id = self._start_run()
        try:
            db.execute(_DDL)
            row = db.query_one("SELECT payload FROM vintage_source_files WHERE name = %s", (IND_SOURCE,))
            if not row:
                raise RuntimeError(f"vintage_source_files has no {IND_SOURCE} — load it once (see module doc)")
            issues: list[dict] = row["payload"]
            code, body = msb._get(msb.INDEX)
            if code != 200:
                raise RuntimeError(f"bulletin index {msb.INDEX} -> HTTP {code}")
            pages = msb.issue_pages(body.decode("utf-8", "replace"))
            todo = new_issues(list(pages), {i["issue"] for i in issues})
            added: list[dict] = []
            for issue in todo:
                iss = self._fetch_issue(msb, issue, pages[issue])
                if iss:
                    added.append(iss)
            if not added:
                # nothing new listed, or a new issue whose indicators file isn't up yet
                self._finish(run_id, "no_new_data", 0, f"checked {todo or 'no new issues'}", None)
                return {"status": "no_new_data", "rows": 0, "checked": todo}
            merged = {i["issue"]: i for i in issues}
            merged.update({i["issue"]: i for i in added})
            payload = [merged[k] for k in sorted(merged)]
            db.execute(
                "UPDATE vintage_source_files SET payload = %s::jsonb, updated_at = now() WHERE name = %s",
                (json.dumps(payload), IND_SOURCE),
            )
            got = [i["issue"] for i in added]
            self._finish(run_id, "success", len(added), f"added {got}", None)
            return {"status": "success", "rows": len(added), "issues": got}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc)[:500], None)
            raise

    def _fetch_issue(self, msb, issue: str, page_url: str) -> dict | None:
        y, m = int(issue[:4]), int(issue[5:])
        code, html = msb._get(page_url)
        if code != 200:
            return None
        links = sorted({msb._abs(h) for h in msb._LINK.findall(html.decode("utf-8", "replace"))})
        mon = msb.FULL_MONTHS[m - 1].lower()
        own = [u for u in links if (str(y) in u or f"-{y % 100:02d}" in u) and mon[:3] in u.lower()]
        # Chapter 1 can carry fewer indicator rows than the full bulletin (Sep-2026: 30 vs
        # 35, the backfill read the full one) — parse every candidate, keep the richest.
        best: dict | None = None
        for url in msb.indicators_links(own):
            code, data = msb._get(url)
            if code != 200 or data[:4] != b"%PDF" or not msb.has_indicator_page(data):
                continue
            with tempfile.TemporaryDirectory() as tmp:
                f = Path(tmp) / f"IND_{y}_{msb.MONTHS[m - 1]}.pdf"
                f.write_bytes(data)
                iss = msb.parse_issue(f)
            if (iss and iss["rows"] and iss["issue"] == issue and issue_lag_ok(iss)
                    and (best is None or len(iss["rows"]) > len(best["rows"]))):
                best = iss
        return best


class VintagesRefreshJob(IngestionJob):
    name = "vintages_refresh"
    source = "PakDataHub"

    def run(self, backfill: bool = False) -> dict:
        from scripts import vintages_core as core

        run_id = self._start_run()
        started = datetime.now(timezone.utc)
        try:
            db.execute(_DDL)
            rows = {r["name"]: r for r in db.query("SELECT name, payload, updated_at FROM vintage_source_files")}
            if not {IND_SOURCE, NA_SOURCE} <= rows.keys():
                raise RuntimeError("vintage_source_files is missing a source — load it once (see module doc)")
            changed = max(r["updated_at"] for r in rows.values())
            last = db.query_one(
                "SELECT max(started_at) AS t FROM ingestion_runs WHERE job_name = %s AND status = 'success'",
                (self.name,),
            )
            if not backfill and last and last["t"] and changed <= last["t"]:
                self._finish(run_id, "no_new_data", 0, None, None)
                return {"status": "no_new_data", "rows": 0}
            with tempfile.TemporaryDirectory() as tmp:
                paths = {}
                for name in (IND_SOURCE, NA_SOURCE):
                    paths[name] = str(Path(tmp) / name)
                    Path(paths[name]).write_text(json.dumps(rows[name]["payload"]), encoding="utf-8")
                try:
                    core.seed([paths[IND_SOURCE]], commit=True)
                    core.apply([paths[IND_SOURCE], paths[NA_SOURCE]], commit=True)
                except SystemExit as exc:  # apply refuses with sys.exit(msg)
                    raise RuntimeError(f"vintages apply refused: {exc}") from None
            # seed's upserts also trip the revision trigger (source NULL) for the bulletin
            # series; apply has just recorded those same changes with their real source.
            bulletin_ids = [s["id"] for s in core.load_specs()]
            db.execute(
                "DELETE FROM observation_revisions WHERE source IS NULL AND series_id = ANY(%s) "
                "AND superseded_at >= %s",
                (bulletin_ids, started),
            )
            n = db.query_one("SELECT count(*) AS n FROM observation_revisions WHERE source IS NOT NULL")["n"]
            self._finish(run_id, "success", int(n), f"rebuilt vintages from sources changed {changed:%Y-%m-%d}", None)
            return {"status": "success", "rows": int(n)}
        except Exception as exc:
            self._finish(run_id, "failed", 0, str(exc)[:500], None)
            raise


def load(paths: list[str]) -> None:
    """One-time: store the parsed source JSONs (by file name) in vintage_source_files."""
    db.execute(_DDL)
    for p in paths:
        payload = json.loads(Path(p).read_text(encoding="utf-8"))
        # stamped with the file's own time, so loading today's sources doesn't make the
        # next vintages_refresh rebuild what is already in the database
        stamp = datetime.fromtimestamp(Path(p).stat().st_mtime, timezone.utc)
        db.execute(
            """INSERT INTO vintage_source_files (name, payload, updated_at) VALUES (%s, %s::jsonb, %s)
               ON CONFLICT (name) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at""",
            (Path(p).name, json.dumps(payload), stamp),
        )
        print(f"stored {Path(p).name}: {len(payload)} issues")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "load":
        load(sys.argv[2:])
    else:
        print(__doc__)
