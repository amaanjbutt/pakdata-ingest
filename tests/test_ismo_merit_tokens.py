"""ismo_merit_order reads its OCR cache from `ismo_merit_tokens` (filled on GitHub Actions)."""
import json

import pytest
from psycopg.types.json import Jsonb

from tests.test_api_integration import _db_available

needs_db = pytest.mark.skipif(not _db_available(), reason="no database reachable")

URL = "https://cmsadmin.ismo.gov.pk/storage/uploads/documents/EMO pytest_ismo_x.pdf"


@needs_db
def test_fetch_builds_the_token_bundle_from_the_table():
    from app import db
    from ingestion.jobs.ismo_merit_order import IsmoMeritOrderJob

    page = {"page": 0, "angle": 0.0, "width": 2000, "height": 3000, "tokens": []}
    db.execute("INSERT INTO ismo_merit_tokens (url, title, published, pages) VALUES (%s, %s, %s, %s) "
               "ON CONFLICT (url) DO NOTHING", (URL, "Economic Merit Order (01-01-2099)", "2099-01-01 10:00:00",
                                               Jsonb([page])))
    try:
        files = IsmoMeritOrderJob().fetch()
        docs = json.loads(files[0].content)["documents"]
        mine = [d for d in docs if d["url"] == URL]
        assert mine == [{"title": "Economic Merit Order (01-01-2099)", "date": "2099-01-01 10:00:00",
                         "url": URL, "pages": [page]}]
        assert docs[-1]["url"] == URL  # ordered by ISMO's upload time
    finally:
        db.execute("DELETE FROM ismo_merit_tokens WHERE url = %s", (URL,))
