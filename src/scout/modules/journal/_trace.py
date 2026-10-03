"""The decisions one request makes, collected while it runs. Like metering, a trace lives in a
context variable, so code anywhere in the request (and threads started with a copy of its
context) can add to it without being handed it."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


class Trace:
    """Events (`kind` plus fields) and page snapshots for one request. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.pages: dict[str, str] = {}

    def add(self, kind: str, **fields: Any) -> None:
        with self._lock:
            self.events.append({"kind": kind, **fields})

    def keep_page(self, name: str, html: str) -> None:
        """Hold a page in memory in case the request ends as a miss worth a snapshot."""
        with self._lock:
            self.pages.setdefault(name, html)


_current: ContextVar[Trace | None] = ContextVar("scout_trace", default=None)


@contextmanager
def tracing() -> Iterator[Trace]:
    trace = Trace()
    token = _current.set(trace)
    try:
        yield trace
    finally:
        _current.reset(token)


def note(kind: str, **fields: Any) -> None:
    """Record a decision in the current request's trace; a no-op outside a request."""
    trace = _current.get()
    if trace is not None:
        trace.add(kind, **fields)


def keep_page(name: str, html: str) -> None:
    trace = _current.get()
    if trace is not None:
        trace.keep_page(name, html)
