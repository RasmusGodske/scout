"""scout's HTTP API. Thin: every endpoint is one `Scout` call; see README.md for the shapes.

    POST /v1/scrape        {"domain", "known_job_ids"?, "fresh"?, "include_raw"?, "budget"?}
    POST /v1/jobs/lookup   {"domain", "job_id", "include_raw"?, "budget"?}

Every scrape and lookup answers with a `request_id` and a `usage` block (Jev calls, tokens and
USD; HTTP requests, rendered pages and bytes; duration), for a platform on top to bill from.
    GET  /v1/routes/{domain}
    GET  /v1/journal?since=30d&domain=&problem=&op=&limit=    JSON Lines
    GET  /v1/journal/report?since=30d                          what to fix next
    GET  /v1/journal/snapshots/{request_id}[/{name}]           pages saved for a miss
    GET  /healthz

Set SCOUT_API_TOKEN to require `Authorization: Bearer <token>` on every /v1 call.
"""

from __future__ import annotations

import hmac
import itertools
import json
import logging
import os
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from scout.app import Scout
from scout.modules.journal import summarize
from scout.modules.metering import Budget

log = logging.getLogger(__name__)


class BudgetBody(BaseModel):
    max_usd: float | None = Field(None, ge=0, description="Cap on money spent (Jev), in USD")
    max_http_requests: int | None = Field(None, ge=0, description="Cap on outgoing HTTP requests")

    def budget(self) -> Budget:
        return Budget(self.max_usd, self.max_http_requests)


class ScrapeBody(BaseModel):
    domain: str = Field(description="The company's domain, e.g. acme.dk")
    known_job_ids: list[str] = Field(
        default_factory=list,
        description="Jobs the caller already has; returned as references without content",
    )
    fresh: bool = Field(False, description="Ignore the cached route and discover from scratch")
    include_raw: bool = Field(True, description="Include each job's raw HTML/feed payload")
    budget: BudgetBody | None = Field(None, description="Stop before passing this; problem budget_exceeded")


class LookupBody(BaseModel):
    domain: str
    job_id: str
    include_raw: bool = True
    budget: BudgetBody | None = None


def create_app(scout: Scout, token: str | None = None) -> FastAPI:
    """`token` (else SCOUT_API_TOKEN) makes every /v1 call require `Authorization: Bearer`.
    Without one the API is open, which is meant for local or isolated networks only."""
    token = token if token is not None else os.environ.get("SCOUT_API_TOKEN") or None
    if token is None:
        log.warning(
            "SCOUT_API_TOKEN is not set: the API accepts requests from anyone who can reach it. "
            "Set it before exposing scout outside a local or isolated network."
        )
    # Scrapes may start a headless browser each; cap how many run at once so a burst of
    # requests queues instead of exhausting memory.
    slots = threading.BoundedSemaphore(int(scout.settings.get("api", {}).get("max_concurrent_requests", 8)))
    app = FastAPI(title="scout", version="1", description="Company domain in, open job listings out.")

    def authorised(authorization: str | None = Header(default=None)) -> None:
        if token is None:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given, token):
            raise HTTPException(status_code=401, detail="missing or wrong bearer token")

    # Sync endpoints: FastAPI runs each on a worker thread, which is what the fetcher's
    # per-thread headless browser and the route cache's lock are built for.

    @app.post("/v1/scrape", dependencies=[Depends(authorised)])
    def scrape(body: ScrapeBody) -> dict[str, Any]:
        budget = body.budget.budget() if body.budget else None
        with slots:
            result = scout.scrape(body.domain, body.known_job_ids, fresh=body.fresh, budget=budget)
        return result.to_dict(with_raw=body.include_raw)

    @app.post("/v1/jobs/lookup", dependencies=[Depends(authorised)])
    def lookup(body: LookupBody) -> dict[str, Any]:
        budget = body.budget.budget() if body.budget else None
        with slots:
            lookup = scout.lookup_job(body.domain, body.job_id, budget=budget)
        return lookup.to_dict(with_raw=body.include_raw)

    @app.get("/v1/routes/{domain}", dependencies=[Depends(authorised)])
    def route(domain: str) -> dict[str, Any]:
        cached = scout.route(domain)
        if cached is None:
            raise HTTPException(status_code=404, detail=f"{domain} has not been scraped")
        out = cached.route.to_dict()
        for key in ("jobs", "route_source", "rediscovered_because", "request_id", "usage"):
            out.pop(key)
        return {
            "route": out,
            "scraped_at": cached.scraped_at,
            "last_ok_at": cached.last_ok_at,
            "verified_at": cached.verified_at,
            "job_count": cached.job_count,
        }

    # --- the journal: what scout did and why, for finding what to fix ---------------------

    @app.get("/v1/journal", dependencies=[Depends(authorised)])
    def journal_entries(
        since: str | None = Query(None, description="YYYY-MM-DD, or a number of days back like 30d"),
        until: str | None = Query(None, description="YYYY-MM-DD"),
        domain: str | None = None,
        problem: str | None = None,
        op: str | None = Query(None, description="scrape or lookup"),
        limit: int | None = Query(None, ge=1),
    ) -> StreamingResponse:
        """Journal entries as JSON Lines, oldest first."""
        entries = scout.journal.entries(_day(since), _day(until), domain=domain, problem=problem, op=op)
        if limit:
            entries = itertools.islice(entries, limit)
        lines = (json.dumps(e, ensure_ascii=False) + "\n" for e in entries)
        return StreamingResponse(lines, media_type="application/x-ndjson")

    @app.get("/v1/journal/report", dependencies=[Depends(authorised)])
    def journal_report(since: str | None = "30d", until: str | None = None) -> dict[str, Any]:
        """The gaps worth fixing: unknown hiring systems, misses, suspicious results, ..."""
        return summarize(scout.journal.entries(_day(since), _day(until)))

    @app.get("/v1/journal/snapshots/{request_id}", dependencies=[Depends(authorised)])
    def snapshot_list(request_id: str) -> dict[str, Any]:
        return {"request_id": request_id, "pages": scout.journal.snapshot_names(request_id)}

    @app.get("/v1/journal/snapshots/{request_id}/{name}", dependencies=[Depends(authorised)])
    def snapshot_page(request_id: str, name: str) -> PlainTextResponse:
        """A saved page, as text (served as text/plain so a browser never runs its scripts)."""
        html = scout.journal.snapshot_page(request_id, name)
        if html is None:
            raise HTTPException(status_code=404, detail="no such snapshot")
        return PlainTextResponse(html)

    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": True, "jev": scout.jev.available}

    return app


def _day(value: str | None) -> date | None:
    """`YYYY-MM-DD` or `<n>d` (n days ago, UTC) -> a date; None stays None."""
    if not value:
        return None
    try:
        if value.endswith("d") and value[:-1].isdigit():
            return datetime.now(UTC).date() - timedelta(days=int(value[:-1]))
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"not a date or <n>d: {value}") from exc
