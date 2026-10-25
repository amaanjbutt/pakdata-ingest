from __future__ import annotations

import os

from ingestion.quota import QuotaCounter


def test_daily_limit_blocks():
    now = 1_000_000.0
    q = QuotaCounter(path=None, daily_limit=2000, daily_frac=0.9, now=lambda: now)
    q.timestamps = [now - i for i in range(1800)]  # 1800 >= 2000*0.9
    assert q.allow() is False


def test_under_daily_limit_allows():
    now = 1_000_000.0
    q = QuotaCounter(path=None, daily_limit=2000, daily_frac=0.9, now=lambda: now)
    q.timestamps = [now - i for i in range(100)]
    assert q.allow() is True


def test_old_timestamps_are_pruned():
    now = 1_000_000.0
    q = QuotaCounter(path=None, daily_limit=2000, daily_frac=0.9, now=lambda: now)
    q.timestamps = [now - 90000 for _ in range(5000)]  # all older than 24h
    assert q.allow() is True
    assert q.count_24h(now) == 0


def test_hourly_limit_sleeps_then_allows():
    now = 1_000_000.0
    slept: list[float] = []
    q = QuotaCounter(
        path=None, daily_limit=2000, hourly_limit=250, hourly_frac=0.9,
        now=lambda: now, sleeper=slept.append,
    )
    # 225 calls in the last hour (>= 250*0.9) but well under the daily cap
    q.timestamps = [now - 10 * i for i in range(225)]
    assert q.allow() is True
    assert slept and slept[0] > 0  # it slept to free the hourly window


def test_record_persists_and_reloads(tmp_path):
    path = os.path.join(tmp_path, "q.json")
    q = QuotaCounter(path=path, now=lambda: 12345.0)
    q.record()
    q.record()
    q2 = QuotaCounter(path=path)
    assert len(q2.timestamps) == 2
