"""Shared base for SBP quarterly-PDF report jobs (Phase 6).

Government quarterly reviews (Payment Systems, Branchless Banking, SME Finance)
are dense multi-table PDFs published on a landing page. This base factors out the
common shape:

  1. discover the report PDF URLs (latest, or historical for backfill),
  2. download + guard against the SBP redesign catch-all (a 200-OK HTML shell
     served for many legacy/asset paths — never a real PDF),
  3. hand pdfplumber pages to the subclass's `parse_pdf`.

Archiving, validation, idempotent upsert, run tracking and alerting all come from
`IngestionJob` (per-file resilience means one off-format historical PDF is
skipped, not fatal). Subclasses implement `report_urls()` and `parse_pdf()`.
"""
from __future__ import annotations

import io
import re
from datetime import date

from ingestion.framework import FetchedFile, IngestionJob, Record


class PdfReportJob(IngestionJob):
    # ---- to be implemented by concrete report jobs --------------------------

    def report_urls(self, backfill: bool) -> list[tuple[str, date]]:
        """Return (pdf_url, report_date) pairs to ingest this run."""
        raise NotImplementedError

    def parse_pdf(self, pages, when: date) -> list[Record]:
        """Parse an opened pdfplumber page list into records."""
        raise NotImplementedError

    # ---- shared plumbing ----------------------------------------------------

    def fetch(self, backfill: bool = False) -> list[FetchedFile]:
        out: list[FetchedFile] = []
        for url, when in self.report_urls(backfill):
            try:
                content = self.http_get(url)
            except Exception:
                continue
            if content[:4] != b"%PDF":  # SBP redesign catch-all shell, not a PDF
                continue
            name = url.rsplit("/", 1)[-1] or "report.pdf"
            out.append(FetchedFile(filename=name, content=content, when=when))
        return out

    def parse(self, f: FetchedFile) -> list[Record]:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(f.content)) as pdf:
            return self.parse_pdf(pdf.pages, f.when)

    # ---- helpers for subclasses --------------------------------------------

    def _links(self, page_url: str, pattern: str) -> list[str]:
        """Absolute hrefs on `page_url` matching `pattern` (order preserved,
        de-duplicated). Returns [] if the page can't be fetched."""
        try:
            html = self.http_get(page_url).decode("utf-8", errors="replace")
        except Exception:
            return []
        seen: dict[str, None] = {}
        for m in re.finditer(r'href="([^"]+)"', html):
            h = m.group(1)
            if re.search(pattern, h, re.I):
                if h.startswith("/"):
                    h = "https://www.sbp.org.pk" + h
                seen.setdefault(h, None)
        return list(seen)
