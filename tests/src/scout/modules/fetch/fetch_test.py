"""Tests for Fetcher: robots.txt, retries, Retry-After and per-host spacing. No real network."""

from __future__ import annotations

import httpx
import pytest

from scout.modules.fetch import Blocked, Fetcher, FetchPolicy, Unreachable
from scout.modules.fetch._throttle import HostThrottle
from support.sites import FakeWeb


def fetcher(web: FakeWeb, **kw: object) -> Fetcher:
    policy = FetchPolicy(**{"user_agent": "scout-bot/0.1", "min_interval_seconds": 0, "retries": 1, **kw})  # type: ignore[arg-type]
    return Fetcher(policy, transport=web.transport())


def test_robots_disallow_is_honoured() -> None:
    web = FakeWeb()
    web.text("https://acme.dk/robots.txt", "User-agent: *\nDisallow: /private\n")
    web.html("https://acme.dk/ok", "<p>hi</p>")
    f = fetcher(web)
    assert f.get("https://acme.dk/ok").ok
    with pytest.raises(Blocked):
        f.get("https://acme.dk/private/x")


def test_missing_robots_allows_and_5xx_robots_disallows() -> None:
    web = FakeWeb()
    web.html("https://a.dk/", "<p>a</p>")
    web.text("https://b.dk/robots.txt", "down", status=503)
    f = fetcher(web)
    assert f.get("https://a.dk/").ok
    with pytest.raises(Blocked):
        f.get("https://b.dk/")


def test_unanswered_host_is_unreachable_not_blocked() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    f = Fetcher(
        FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=httpx.MockTransport(down)
    )
    with pytest.raises(Unreachable):
        f.get("https://gone.dk/")


def test_retries_transient_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, text="ok")

    web = FakeWeb()
    web.handler("https://a.dk/feed", flaky)
    assert fetcher(web).get("https://a.dk/feed").text == "ok"


def test_long_retry_after_gives_up_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda s: None)
    web = FakeWeb()
    web.handler("https://a.dk/feed", lambda r: httpx.Response(429, headers={"retry-after": "3600"}))
    f = fetcher(web, retries=3)
    assert f.get("https://a.dk/feed").status == 429
    assert web.urls().count("https://a.dk/feed") == 1


def test_throttle_spaces_requests_per_host() -> None:
    now = {"t": 0.0}
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now["t"] += s

    throttle = HostThrottle(1.0, clock=lambda: now["t"], sleep=sleep, per_host={"slow.com": 4.0})
    for host in ("a.dk", "a.dk", "b.dk", "slow.com", "slow.com"):
        throttle.wait(host)
    assert slept == [1.0, 4.0]
