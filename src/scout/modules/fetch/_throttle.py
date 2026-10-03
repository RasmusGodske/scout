from __future__ import annotations

import threading
import time
from collections.abc import Callable


class HostThrottle:
    """Spaces requests to the same host at least `interval` seconds apart, across threads."""

    def __init__(
        self,
        interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        per_host: dict[str, float] | None = None,
    ) -> None:
        self._interval = interval
        self._per_host = {h.lower(): v for h, v in (per_host or {}).items()}
        self._clock = clock
        self._sleep = sleep
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            last = self._last.get(host)
            if last is not None:
                interval = self._per_host.get(host, self._interval)
                remaining = interval - (self._clock() - last)
                if remaining > 0:
                    self._sleep(remaining)
            self._last[host] = self._clock()

    def back_off(self, host: str, seconds: float) -> None:
        """Push the host's next slot `seconds` into the future (a Retry-After)."""
        with self._guard:
            self._last[host] = max(self._last.get(host, 0.0), self._clock() + seconds - self._interval)
