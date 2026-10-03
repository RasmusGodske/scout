from __future__ import annotations

import csv
import logging
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.modules.discovery import Known, Resolver, normalize_domain
from scout.modules.fetch import Fetcher
from scout.modules.metering import Budget, metered
from scout.modules.results import JobLookup, ListingStatus, ScrapeResult, Status, Usage
from scout.modules.routes import RouteCache

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScrapeRequest:
    domain: str
    known_job_ids: frozenset[str] = frozenset()
    fresh: bool = False
    budget: Budget | None = None


class Scraper:
    """Scrape companies and look up jobs, keeping the route cache current.

    The only code that reads and writes the route cache around a scrape: it hands the resolver
    what is known (cached route, whether it is stale, cached link verdicts) and saves the
    route the resolver comes back with. Job listings pass through; nothing about them is kept.

    Every scrape and lookup is one metered request: it gets a `request_id` and a `usage` of
    what it consumed, and stops at its `budget` if one is given.
    """

    def __init__(
        self,
        resolver: Resolver,
        fetcher: Fetcher,
        routes: RouteCache,
        *,
        route_max_age_days: float = 30,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._resolver = resolver
        self._fetcher = fetcher
        self._routes = routes
        self._max_age = timedelta(days=route_max_age_days)
        self._clock = clock

    def scrape(
        self,
        domain: str,
        known_job_ids: Iterable[str] = (),
        *,
        fresh: bool = False,
        budget: Budget | None = None,
    ) -> ScrapeResult:
        """Every job the company lists now. Jobs in `known_job_ids` come back as references
        (`known: true`, no content), so a caller that stores jobs can skip refetching them."""
        request_id = new_request_id()
        with metered(budget) as meter:
            result = self._scrape(normalize_domain(domain), known_job_ids, fresh)
        result = replace(result, request_id=request_id, usage=meter.usage())
        _log_usage(request_id, "scrape", result.domain, result.usage)
        return result

    def lookup_job(self, domain: str, job_id: str, *, budget: Budget | None = None) -> JobLookup:
        """One job's current content from the company's domain and the job's id alone. A
        company without a cached route is scraped first to find one (within the same budget)."""
        request_id = new_request_id()
        with metered(budget) as meter:
            lookup = self._lookup(normalize_domain(domain), job_id)
        lookup = replace(lookup, request_id=request_id, usage=meter.usage())
        _log_usage(request_id, "lookup", lookup.domain, lookup.usage)
        return lookup

    def _scrape(self, domain: str, known_job_ids: Iterable[str], fresh: bool) -> ScrapeResult:
        result = self._resolver.resolve(domain, self._known(domain, known_job_ids), fresh=fresh)
        self._routes.save(result, self._clock(), verified=result.route_verified)
        return result

    def _lookup(self, domain: str, job_id: str) -> JobLookup:
        cached = self._routes.get(domain)
        if cached is None or cached.route.status not in (Status.BOARD, Status.PAGE):
            result = self._scrape(domain, (), False)
            if result.status not in (Status.BOARD, Status.PAGE):
                return JobLookup(
                    domain,
                    job_id,
                    ListingStatus.CHECK_FAILED,
                    problem=result.problem,
                    reason=result.reason or f"no route to this company's jobs ({result.status.value})",
                )
            cached = self._routes.get(domain)
            assert cached is not None
        return self._resolver.lookup_job(cached.route, job_id)

    def scrape_many(
        self,
        requests: Iterable[ScrapeRequest],
        *,
        workers: int = 8,
        on_result: Callable[[ScrapeResult], None] | None = None,
    ) -> list[ScrapeResult]:
        """Scrape many companies on a thread pool; `on_result` sees each result as it lands."""
        results: list[ScrapeResult] = []

        def work(request: ScrapeRequest) -> ScrapeResult:
            try:
                return self.scrape(
                    request.domain, request.known_job_ids, fresh=request.fresh, budget=request.budget
                )
            finally:
                self._fetcher.close_thread()

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            queue = iter(requests)
            pending: set[Future[ScrapeResult]] = set()

            def fill() -> None:
                for request in queue:
                    pending.add(pool.submit(work, request))
                    if len(pending) >= workers * 2:
                        return

            fill()
            while pending:
                done, still = wait(pending, return_when=FIRST_COMPLETED)
                pending.clear()
                pending.update(still)
                for future in done:
                    result = future.result()
                    results.append(result)
                    if on_result:
                        on_result(result)
                fill()
        return results

    def _known(self, domain: str, known_job_ids: Iterable[str]) -> Known:
        cached = self._routes.get(domain)
        if cached is None:
            return Known(job_ids=frozenset(known_job_ids))
        verified = _parse(cached.verified_at)
        stale = verified is None or self._clock() - verified > self._max_age
        return Known(
            previous=cached.route,
            previous_job_count=cached.job_count,
            route_stale=stale,
            job_ids=frozenset(known_job_ids),
            link_verdicts=self._routes.link_verdicts(domain),
        )


def read_domains(path: str | Path, column: str = "domain") -> list[str]:
    """Domains from a CSV with a `domain` (or `website`/`url`) column, or one per line."""
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if not lines:
        return []
    header = [h.strip().lower() for h in next(csv.reader([lines[0]]))]
    for name in (column, "domain", "website", "company domain", "url"):
        if name in header:
            rows = csv.DictReader(lines, fieldnames=header)
            next(rows)
            values = [r.get(name) or "" for r in rows]
            break
    else:
        values = lines
    out = [normalize_domain(v) for v in values if v and v.strip()]
    return list(dict.fromkeys(d for d in out if "." in d))


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:24]


def _log_usage(request_id: str, op: str, domain: str, usage: Usage) -> None:
    log.info(
        "%s %s %s: jev %d calls $%.6f, http %d requests (%d rendered) %d bytes, %d ms",
        request_id,
        op,
        domain,
        usage.jev.calls,
        usage.jev.usd,
        usage.http.requests,
        usage.http.rendered_pages,
        usage.http.bytes,
        usage.duration_ms,
    )


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
