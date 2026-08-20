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


class QuotaPool:
    """Rotate across several EasyData API keys, each with its own QuotaCounter.

    The effective hourly/daily budget scales with the number of keys, which is
    what breaks the meta-check starvation: a single key's 250/hour cap is smaller
    than the ~226 dataset freshness meta-calls a run makes, so with one key the
    meta sweep consumes the whole hourly budget before any data is pulled. With N
    keys the pool hands each call to whichever key still has room, so the sweep
    spreads out and data pulls keep budget.

    Drop-in for the single-counter interface the job uses: ``allow()`` /
    ``record()`` plus ``api_key`` for the currently-selected key.
    """

    def __init__(
        self,
        keys: list[str],
        base_path: str | None = None,
        daily_limit: int = 2000,
        hourly_limit: int = 250,
        daily_frac: float = 0.9,
        hourly_frac: float = 0.9,
    ) -> None:
        if not keys:
            raise ValueError("QuotaPool needs at least one key")
        self.keys = list(keys)
        self._counters: list[QuotaCounter] = []
        for i, _ in enumerate(self.keys):
            path = None
            if base_path:
                root, ext = os.path.splitext(base_path)
                path = f"{root}.{i}{ext or '.json'}"
            self._counters.append(
                QuotaCounter(path, daily_limit, hourly_limit, daily_frac, hourly_frac)
            )
        self._active = 0
        # Per-key cooldown: when a key returns a real 429 from EasyData (its
        # server-side quota is spent — which the local counter can't see, since the
        # CI counter file is ephemeral), penalize() blocks that key here so allow()
        # rotates to another key instead of hammering the exhausted one.
        self._blocked_until = [0.0] * len(self.keys)

    @property
    def api_key(self) -> str:
        return self.keys[self._active]

    def allow(self) -> bool:
        """True if any key can serve a call now; selects that key as active.

        Honours EASYDATA_QUOTA_NOSLEEP exactly like QuotaCounter: on a time-boxed
        CI runner, return False instead of sleeping when every key is momentarily
        hour-capped (the run marks 'partial' and the next run resumes)."""
        now = time.time()
        # Fast path: a key that is under both its daily and hourly caps right now.
        for i, c in enumerate(self._counters):
            c._prune(now)
            if now < self._blocked_until[i]:
                continue  # key got a 429 recently — skip until its cooldown passes
            if c._daily_exhausted(now):
                continue
            if c.count_1h(now) < c.hourly_limit * c.hourly_frac:
                self._active = i
                return True
        if os.getenv("EASYDATA_QUOTA_NOSLEEP"):
            return False
        # Every key with daily budget is hour-capped: sleep until the one that
        # frees soonest opens up, then re-check its daily cap.
        best: tuple[int, float] | None = None
        for i, c in enumerate(self._counters):
            if c._daily_exhausted(now):
                continue
            hour_ts = [t for t in c.timestamps if t > now - 3600]
            free_at = (min(hour_ts) + 3600 + 1) if hour_ts else now
            free_at = max(free_at, self._blocked_until[i])  # respect 429 cooldown
            if best is None or free_at < best[1]:
                best = (i, free_at)
        if best is None:
            return False  # all keys daily-exhausted
        sleep_for = best[1] - now
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._active = best[0]
        now = time.time()
        self._counters[self._active]._prune(now)
        return not self._counters[self._active]._daily_exhausted(now)

    def record(self) -> None:
        self._counters[self._active].record()

    def penalize(self, seconds: float = 3600.0) -> None:
        """Mark the active key as server-side rate-limited (it returned a 429) so
        allow() skips it for `seconds` and rotates to another key. One hour matches
        EasyData's hourly window and effectively retires a spent key for the rest of
        a time-boxed CI run."""
        self._blocked_until[self._active] = time.time() + seconds
