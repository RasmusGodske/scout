"""Tests for metering: counting, budgets checked before spending, and threads joining a request."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest

from scout.modules.metering import Budget, BudgetExceeded, current, metered


def test_counts_and_reports_usage() -> None:
    with metered() as meter:
        meter.before_http()
        meter.http(100)
        meter.before_http(rendered=True)
        meter.http(50)
        meter.jev(300, 20, 0.0000126)
    usage = meter.usage()
    assert (usage.http.requests, usage.http.rendered_pages, usage.http.bytes) == (1, 1, 150)
    assert (usage.jev.calls, usage.jev.input_tokens, usage.jev.usd) == (1, 300, 0.0000126)
    assert usage.duration_ms >= 0


def test_budget_is_checked_before_spending() -> None:
    with metered(Budget(max_usd=0.001, max_http_requests=2)) as meter:
        meter.before_http()
        meter.before_http(rendered=True)  # a render counts against the cap too
        with pytest.raises(BudgetExceeded):
            meter.before_http()
        meter.before_jev()
        meter.jev(1, 1, 0.002)
        with pytest.raises(BudgetExceeded):
            meter.before_jev()


def test_no_meter_outside_a_request_and_threads_join_the_request() -> None:
    assert current() is None
    with metered() as meter, ThreadPoolExecutor(4) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, lambda: current().before_http()) for _ in range(8)
        ]  # type: ignore[union-attr]
        for f in futures:
            f.result()
    assert meter.usage().http.requests == 8
    assert current() is None


def test_parallel_threads_cannot_overshoot_the_budget() -> None:
    def try_request() -> bool:
        try:
            current().before_http()  # type: ignore[union-attr]
            return True
        except BudgetExceeded:
            return False

    with metered(Budget(max_http_requests=5)) as meter, ThreadPoolExecutor(16) as pool:
        made = [
            f.result() for f in [pool.submit(contextvars.copy_context().run, try_request) for _ in range(100)]
        ]
    assert sum(made) == 5 and meter.usage().http.requests == 5
