from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from lxml import html as lxml_html

from scout.modules import metering
from scout.modules.fetch._browser import Browser
from scout.modules.fetch._throttle import HostThrottle

log = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504, 529}


@dataclass(frozen=True)
class FetchPolicy:
    user_agent: str
    min_interval_seconds: float = 1.0
    timeout_seconds: float = 20.0
    retries: int = 2
    honour_robots: bool = True
    max_bytes: int = 5_000_000
    render_min_text_chars: int = 400
    render_min_links: int = 5
    render_wait_ms: int = 2500
    # Hosts that want slower traffic than min_interval_seconds, e.g. vendor APIs that
    # rate-limit (Workable answered 429 at about one request per second).
    host_intervals: dict[str, float] = field(default_factory=dict)
    max_retry_after_seconds: float = 60.0

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> FetchPolicy:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


@dataclass(frozen=True)
class Response:
    url: str
    final_url: str
    status: int
    text: str
    content_type: str = ""
    rendered: bool = False
    headers_retry_after: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


class FetchError(Exception):
    """The request could not be completed: network failure, timeout, or a response too large."""


class Blocked(FetchError):
    """robots.txt disallows the URL for our user agent."""


class Unreachable(FetchError):
    """The host did not answer (DNS, connection, timeout) after retries."""


class Fetcher:
    """The only code that talks to the network.

    Every request is throttled per host, checked against that host's robots.txt, retried with
    backoff on transient failures, and sent with our bot user agent. `page()` additionally
    retries once in a headless browser when a plain fetch comes back near-empty.

    `transport` replaces the network in tests; with it set, the browser is never started.
    """

    def __init__(self, policy: FetchPolicy, transport: httpx.BaseTransport | None = None) -> None:
        self._policy = policy
        self._client = httpx.Client(
            headers={"User-Agent": policy.user_agent, "Accept-Language": "da,en;q=0.8"},
            timeout=policy.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )
        self._throttle = HostThrottle(policy.min_interval_seconds, per_host=policy.host_intervals)
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = threading.Lock()
        self._browser = (
            None
            if transport is not None
            else Browser(policy.user_agent, int(policy.timeout_seconds * 1000), policy.render_wait_ms)
        )

    # --- public -------------------------------------------------------------------------------

    def get(self, url: str, *, accept: str = "*/*", referer: str | None = None) -> Response:
        """Plain GET. Non-2xx statuses come back as a Response; network failures raise FetchError."""
        headers = {"Accept": accept}
        if referer:
            headers["Referer"] = referer
        return self._request("GET", url, headers=headers)

    def post_json(self, url: str, payload: Any) -> Response:
        return self._request(
            "POST",
            url,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            content=json.dumps(payload),
        )

    def page(self, url: str, *, render: bool = False) -> Response:
        """GET an HTML page; if it is near-empty, or `render` is set, render it once in the
        headless browser. A rendered page that fails to load falls back to the plain one."""
        response = self.get(url, accept="text/html,application/xhtml+xml")
        if not response.ok or self._browser is None:
            return response
        if not render and not self._looks_empty(response.text):
            return response
        meter = metering.current()
        try:
            self._allow(url)
            if meter is not None:
                meter.before_http(rendered=True)
            self._throttle.wait(_host(url))
            final_url, html = self._browser.render(url)
        except (Blocked, metering.BudgetExceeded):
            raise
        except Exception as exc:  # noqa: BLE001 - the plain response is still a valid answer
            log.info("render failed for %s: %s", url, exc)
            return response
        log.info("rendered %s in the browser", url)
        if meter is not None:
            meter.http(len(html.encode("utf-8", errors="replace")))
        return Response(url, final_url, 200, html, "text/html", rendered=True)

    def close_thread(self) -> None:
        """Close the calling thread's headless browser, if it started one."""
        if self._browser is not None:
            self._browser.close()

    def close(self) -> None:
        self.close_thread()
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- internal -----------------------------------------------------------------------------

    def _request(self, method: str, url: str, **kwargs: Any) -> Response:
        self._allow(url)
        attempt = 0
        while True:
            self._throttle.wait(_host(url))
            try:
                response = self._send(method, url, **kwargs)
            except (httpx.TransportError, FetchError) as exc:
                if attempt >= self._policy.retries or isinstance(exc, _TooLarge):
                    kind = FetchError if isinstance(exc, _TooLarge) else Unreachable
                    raise kind(f"{method} {url}: {exc.__class__.__name__}: {exc}") from exc
            else:
                if response.status not in _RETRY_STATUSES or attempt >= self._policy.retries:
                    return response
                wait = _retry_after(response.headers_retry_after)
                if wait is not None:
                    if wait > self._policy.max_retry_after_seconds:
                        return response  # not worth holding a worker this long
                    self._throttle.back_off(_host(url), wait)
            attempt += 1
            time.sleep(min(2**attempt, 10))

    def _send(self, method: str, url: str, **kwargs: Any) -> Response:
        meter = metering.current()
        if meter is not None:
            meter.before_http()
        response = self._send_unmetered(method, url, **kwargs)
        if meter is not None:
            meter.http(len(response.text.encode("utf-8", errors="replace")))
        return response

    def _send_unmetered(self, method: str, url: str, **kwargs: Any) -> Response:
        with self._client.stream(method, url, **kwargs) as r:
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) > self._policy.max_bytes:
                    raise _TooLarge(f"more than {self._policy.max_bytes} bytes")
            encoding = r.encoding or "utf-8"
            text = bytes(body).decode(encoding, errors="replace")
            return Response(
                url,
                str(r.url),
                r.status_code,
                text,
                r.headers.get("content-type", ""),
                headers_retry_after=r.headers.get("retry-after", ""),
            )

    def _allow(self, url: str) -> None:
        if not self._policy.honour_robots:
            return
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._robots_lock:
            known = origin in self._robots
            parser = self._robots.get(origin)
        if not known:
            # An unreachable host raises here and is not cached, so a later call retries it.
            # Loaded outside the lock so threads probing different hosts don't queue behind it;
            # two threads may load the same file once each, which is harmless.
            parser = self._load_robots(origin)
            with self._robots_lock:
                self._robots[origin] = parser
        if parser is not None and not parser.can_fetch(self._policy.user_agent, url):
            raise Blocked(f"robots.txt disallows {url}")

    def _load_robots(self, origin: str) -> RobotFileParser | None:
        """RFC 9309: a 4xx means no rules, a 5xx means everything is disallowed. A host that
        does not answer at all raises FetchError instead, so it reads as unreachable rather
        than as a refusal."""
        parser = RobotFileParser()
        last: Exception | None = None
        for attempt in range(self._policy.retries + 1):
            try:
                self._throttle.wait(urlsplit(origin).netloc)
                r = self._send("GET", origin + "/robots.txt", headers={"Accept": "text/plain"})
                break
            except (httpx.TransportError, FetchError) as exc:
                last = exc
                time.sleep(min(2 ** (attempt + 1), 10) if attempt < self._policy.retries else 0)
        else:
            raise Unreachable(f"{origin} unreachable: {last.__class__.__name__}: {last}")
        if 400 <= r.status < 500:
            return None
        if r.status >= 500:
            parser.parse(["User-agent: *", "Disallow: /"])
            return parser
        parser.parse(r.text.splitlines())
        return parser

    def _looks_empty(self, html: str) -> bool:
        try:
            doc = lxml_html.fromstring(html)
        except (ValueError, lxml_html.etree.ParserError):
            return True
        for bad in doc.xpath("//script|//style|//noscript|//template"):
            bad.drop_tree()
        text = " ".join(doc.text_content().split())
        links = doc.xpath("//a[@href]")
        return len(text) < self._policy.render_min_text_chars or len(links) < self._policy.render_min_links


class _TooLarge(FetchError):
    pass


def _retry_after(value: str) -> float | None:
    """Seconds from a Retry-After header in its delay form; the date form is ignored."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower()
