"""Small, process-local request guard for the demo Function URL.

The hosted console is intentionally deployed without API Gateway or a WAF.
This limiter provides a bounded per-execution-environment guard for login and
API traffic. Lambda reserved concurrency remains the account-level ceiling;
production deployments should replace this with a distributed edge control.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
import time


@dataclass(slots=True)
class _Window:
    count: int
    reset_at: float


class InMemoryRateLimiter:
    """Fixed-window limiter with bounded, expiring key state."""

    def __init__(self, *, max_keys: int = 4096) -> None:
        self._max_keys = max_keys
        self._windows: dict[str, _Window] = {}
        self._lock = RLock()

    def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: int,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Return ``(allowed, retry_after_seconds)`` for one request."""

        if limit <= 0 or window_seconds <= 0:
            return True, 0
        current = time.monotonic() if now is None else now
        with self._lock:
            self._evict_expired(current)
            window = self._windows.get(key)
            if window is None or window.reset_at <= current:
                self._windows[key] = _Window(count=1, reset_at=current + window_seconds)
                self._trim_if_needed()
                return True, 0
            if window.count >= limit:
                return False, max(1, int(window.reset_at - current + 0.999))
            window.count += 1
            return True, 0

    def _evict_expired(self, now: float) -> None:
        expired = [key for key, window in self._windows.items() if window.reset_at <= now]
        for key in expired:
            self._windows.pop(key, None)

    def _trim_if_needed(self) -> None:
        if len(self._windows) <= self._max_keys:
            return
        oldest = min(self._windows, key=lambda key: self._windows[key].reset_at)
        self._windows.pop(oldest, None)


rate_limiter = InMemoryRateLimiter()
