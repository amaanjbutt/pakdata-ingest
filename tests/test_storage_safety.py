from __future__ import annotations

import os
from datetime import date

import pytest

from app.config import settings
from ingestion import storage


def test_traversal_filename_stays_inside_base(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_archive_dir", str(tmp_path))
    path = storage.archive("SBP", "job", date(2026, 7, 16), "../../etc/x", b"data")
    base = os.path.realpath(str(tmp_path))
    assert os.path.realpath(path).startswith(base + os.sep)
    # nothing escaped to an absolute traversal target
    assert not os.path.exists("/etc/x")
    assert os.path.exists(path)


def test_pure_dotdot_component_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_archive_dir", str(tmp_path))
    with pytest.raises(ValueError):
        storage.archive_path("SBP", "..", date(2026, 7, 16), "x")


def test_traversal_source_stays_inside_base(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "raw_archive_dir", str(tmp_path))
    path = storage.archive_path("../../../evil", "job", date(2026, 7, 16), "f.json")
    base = os.path.realpath(str(tmp_path))
    assert os.path.realpath(path).startswith(base + os.sep)
