"""Persisted, rate-limit-aware call counter for the EasyData API.

EasyData caps a key at 2000 requests/day and 250/hour across all calls. This
counter persists request timestamps to a JSON file so limits are respected
across process restarts (a multi-day backfill can stop and resume safely).

Policy (fractions leave headroom):
  - If calls in the last 24h reach daily_limit * daily_frac -> allow() returns
    False (caller should stop cleanly and mark the run 'partial').
  - If calls in the last hour reach hourly_limit * hourly_frac -> allow() sleeps
    until the hourly window frees, then re-checks the daily cap.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable


class QuotaCounter:
    def __init__(
        self,
        path: str | None,
        daily_limit: int = 2000,
        hourly_limit: int = 250,
        daily_frac: float = 0.9,
        hourly_frac: float = 0.9,
        now: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path
        self.daily_limit = daily_limit
        self.hourly_limit = hourly_limit
        self.daily_frac = daily_frac
        self.hourly_frac = hourly_frac
        self._now = now
        self._sleep = sleeper
        self.timestamps: list[float] = self._load()

    # ---- persistence --------------------------------------------------------

    def _load(self) -> list[float]:
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    return [float(t) for t in json.load(f).get("timestamps", [])]
            except (ValueError, OSError):
                return []
        return []

    def _persist(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"timestamps": self.timestamps}, f)

    # ---- counting -----------------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - 86400
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def count_24h(self, now: float | None = None) -> int:
        now = self._now() if now is None else now
        return sum(1 for t in self.timestamps if t > now - 86400)

    def count_1h(self, now: float | None = None) -> int:
        now = self._now() if now is None else now
        return sum(1 for t in self.timestamps if t > now - 3600)

    def _daily_exhausted(self, now: float) -> bool:
        return self.count_24h(now) >= self.daily_limit * self.daily_frac

    def allow(self) -> bool:
        """True if another call may be made now. May sleep to respect the hourly
        window. Returns False when the daily budget is exhausted.

        Set EASYDATA_QUOTA_NOSLEEP=1 (e.g. on a time-boxed CI runner) to stop
        cleanly at the hourly cap instead of sleeping ~1h — the caller marks the
        run 'partial' and the next scheduled run resumes in the fresh hour window.
        """
        now = self._now()
        self._prune(now)
        if self._daily_exhausted(now):
            return False
        if self.count_1h(now) >= self.hourly_limit * self.hourly_frac:
            if os.getenv("EASYDATA_QUOTA_NOSLEEP"):
                return False
            hour_ts = [t for t in self.timestamps if t > now - 3600]
            sleep_for = 3600 - (now - min(hour_ts)) + 1
            if sleep_for > 0:
                self._sleep(sleep_for)
            now = self._now()
            self._prune(now)
            if self._daily_exhausted(now):
                return False
        return True

    def record(self) -> None:
        self.timestamps.append(self._now())
        self._persist()
