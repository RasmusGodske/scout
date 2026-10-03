from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from scout.modules.results import HttpUsage, JevUsage, Usage


@dataclass(frozen=True)
class Budget:
    """A ceiling on one request. `max_usd` caps money actually spent (Jev); `max_http_requests`
    caps outgoing requests, headless renders included (every page, feed, robots.txt and retry
    counts). None = no cap. Jev is checked before each call, so one call may end past max_usd."""

    max_usd: float | None = None
    max_http_requests: int | None = None


class BudgetExceeded(Exception):
    """The request's budget would be passed by the next call; nothing more is spent."""


class Meter:
    """What one request consumed so far. Shared by every thread working on that request."""

    def __init__(self, budget: Budget | None = None) -> None:
        self._budget = budget or Budget()
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._http_requests = 0
        self._http_bytes = 0
        self._rendered = 0
        self._jev_calls = 0
        self._jev_in = 0
        self._jev_out = 0
        self._jev_usd = 0.0

    # Checks come before spending: a call that would pass the budget is not made. An HTTP slot is
    # reserved in the same step as the check, so parallel threads cannot all slip under the cap.

    def before_http(self, *, rendered: bool = False) -> None:
        """Reserve one outgoing request (or headless render) against the budget."""
        cap = self._budget.max_http_requests
        with self._lock:
            if cap is not None and self._http_requests + self._rendered >= cap:
                raise BudgetExceeded(f"budget of {cap} HTTP requests reached")
            if rendered:
                self._rendered += 1
            else:
                self._http_requests += 1

    def before_jev(self) -> None:
        cap = self._budget.max_usd
        with self._lock:
            if cap is not None and self._jev_usd >= cap:
                raise BudgetExceeded(f"budget of ${cap} reached (${self._jev_usd:.6f} spent)")

    def http(self, nbytes: int) -> None:
        """Bytes downloaded by a request already reserved with `before_http`."""
        with self._lock:
            self._http_bytes += nbytes

    def jev(self, input_tokens: int, output_tokens: int, usd: float) -> None:
        with self._lock:
            self._jev_calls += 1
            self._jev_in += input_tokens
            self._jev_out += output_tokens
            self._jev_usd += usd

    def usage(self) -> Usage:
        with self._lock:
            return Usage(
                jev=JevUsage(self._jev_calls, self._jev_in, self._jev_out, round(self._jev_usd, 8)),
                http=HttpUsage(self._http_requests, self._rendered, self._http_bytes),
                duration_ms=int((time.monotonic() - self._started) * 1000),
            )


_current: ContextVar[Meter | None] = ContextVar("scout_meter", default=None)


@contextmanager
def metered(budget: Budget | None = None) -> Iterator[Meter]:
    """Meter everything done inside the block (in this thread, and in threads started with a
    copy of this context) against one Meter."""
    meter = Meter(budget)
    token = _current.set(meter)
    try:
        yield meter
    finally:
        _current.reset(token)


def current() -> Meter | None:
    """The meter of the request being served, or None outside any request."""
    return _current.get()
