"""Fixed-window rate limiting for the login route (DESIGN.md §8).

In-process and per-worker, like SessionStore -- adequate for v1's single worker,
and the obvious thing to move to Redis alongside the session store if that changes.
Only *failed* attempts count, so a user who signs in correctly is never throttled.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """Allow at most `attempts` failures per key within a rolling window."""

    def __init__(
        self,
        attempts: int,
        window_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float]:
        hits = self._hits.setdefault(key, deque())
        cutoff = self._clock() - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def allowed(self, key: str) -> bool:
        """True if another attempt may be made. Does not record anything."""
        return len(self._recent(key)) < self.attempts

    def record_failure(self, key: str) -> None:
        self._recent(key)
        self._hits.setdefault(key, deque()).append(self._clock())

    def reset(self, key: str) -> None:
        """Clear a key's history -- called on a successful sign-in."""
        self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        """Seconds until the oldest attempt in the window ages out."""
        hits = self._recent(key)
        if len(hits) < self.attempts:
            return 0
        return max(1, int(hits[0] + self.window_seconds - self._clock()) + 1)

    def sweep(self) -> int:
        """Drop keys whose windows have fully expired."""
        before = len(self._hits)
        for key in list(self._hits):
            self._recent(key)
        return before - len(self._hits)

    def __len__(self) -> int:
        return len(self._hits)
