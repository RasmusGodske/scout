"""A fake internet for tests: map URLs to responses and serve them through httpx.MockTransport.

Lives at tests/support because every module's tests that need HTTP use it (fetch, boards,
discovery, jev); it is scaffolding, not a subject.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

Handler = Callable[[httpx.Request], httpx.Response]


class FakeWeb:
    """Routes by exact URL (with query), then by URL without query. Unknown URLs are 404.

    `robots.txt` answers 404 (no rules) unless a test sets one. Every request is recorded.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []

    def html(self, url: str, body: str, status: int = 200) -> None:
        self.routes[url] = lambda r: httpx.Response(status, text=body, headers={"content-type": "text/html"})

    def json(self, url: str, data: Any, status: int = 200) -> None:
        self.routes[url] = lambda r: httpx.Response(status, json=data)

    def text(self, url: str, body: str, status: int = 200, content_type: str = "text/plain") -> None:
        self.routes[url] = lambda r: httpx.Response(status, text=body, headers={"content-type": content_type})

    def redirect(self, url: str, to: str) -> None:
        self.routes[url] = lambda r: httpx.Response(302, headers={"location": to})

    def handler(self, url: str, fn: Handler) -> None:
        self.routes[url] = fn

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._serve)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    def _serve(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        handler = self.routes.get(url) or self.routes.get(url.split("?")[0])
        if handler is None:
            return httpx.Response(404, text="not found")
        return handler(request)


def jev_transport(
    answer: Callable[[dict[str, Any]], dict[str, Any]], seen: list[dict[str, Any]] | None = None
) -> httpx.MockTransport:
    """A fake /v1/systemone: `answer(question)` returns one answer per question."""

    def serve(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if seen is not None:
            seen.append(body)
        answers = {qid: answer(q) for qid, q in body["questions"].items()}
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "answers": answers,
                "usage": {"input_tokens": 100 * len(answers), "output_tokens": 10, "cost": 0.000004},
            },
        )

    return httpx.MockTransport(serve)
