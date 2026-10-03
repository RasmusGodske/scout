"""Tests for Scraper: the route cache around each scrape, staleness, known ids, lookups by the
two ids, and many companies at once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout.modules.boards import Boards, BoardsConfig
from scout.modules.discovery import DiscoveryConfig, Resolver
from scout.modules.fetch import Fetcher, FetchPolicy
from scout.modules.jev import Jev, JevConfig
from scout.modules.metering import Budget
from scout.modules.results import ListingStatus, Problem, RouteSource, Status
from scout.modules.routes import RouteCache
from scout.modules.scraper import Scraper, ScrapeRequest, read_domains
from support.sites import FakeWeb

FILLER = "<a href=/n1>a</a><a href=/n2>b</a><a href=/n3>c</a><a href=/n4>d</a><a href=/n5>e</a>" + "x" * 600


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def scraper(web: FakeWeb, tmp: Path, clock: Clock | None = None) -> tuple[Scraper, RouteCache]:
    fetcher = Fetcher(
        FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=web.transport()
    )
    boards = Boards(fetcher, BoardsConfig(guess_systems=["greenhouse"], slug_suffixes=[]))
    jev = Jev(JevConfig(model="typesafe/jev-1.13-20260917", usage_log=str(tmp / "u.jsonl")), None)
    config = DiscoveryConfig(careers_words=["karriere", "jobs"], common_paths=["/karriere"])
    routes = RouteCache(":memory:")
    return Scraper(Resolver(fetcher, boards, jev, config), fetcher, routes, clock=clock or Clock()), routes


def site(web: FakeWeb, domain: str) -> None:
    web.html(f"https://{domain}/", f"<a href='/karriere'>Karriere</a>{FILLER}")
    web.html(f"https://{domain}/karriere", f"<a href='/karriere/kok-1'>Kok</a>{FILLER}")
    web.html(f"https://{domain}/karriere/kok-1", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")


def test_read_domains(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("Company,Website\nAcme,https://www.Acme.dk/\nB,b.dk\nB again,b.dk\n,\n")
    assert read_domains(tmp_path / "a.csv", "website") == ["acme.dk", "b.dk"]
    (tmp_path / "b.txt").write_text("acme.dk\nb.dk\n")
    assert read_domains(tmp_path / "b.txt") == ["acme.dk", "b.dk"]


def test_the_route_is_cached_and_reused(tmp_path: Path) -> None:
    web = FakeWeb()
    site(web, "a.dk")
    s, routes = scraper(web, tmp_path)
    first = s.scrape("a.dk")
    assert first.route_source is RouteSource.DISCOVERED and routes.get("a.dk").route.status is Status.PAGE  # type: ignore[union-attr]
    web.requests.clear()
    again = s.scrape("a.dk", known_job_ids=["https://a.dk/karriere/kok-1"])
    assert again.route_source is RouteSource.CACHED
    assert web.urls() == ["https://a.dk/karriere"]  # one fetch: the careers page, nothing else
    assert again.jobs[0].known and again.jobs[0].content is None


def test_an_old_route_is_rechecked_against_the_homepage(tmp_path: Path) -> None:
    web = FakeWeb()
    site(web, "a.dk")
    clock = Clock()
    s, routes = scraper(web, tmp_path, clock)
    s.scrape("a.dk")
    clock.now += timedelta(days=31)
    web.requests.clear()
    s.scrape("a.dk")
    assert "https://a.dk/" in web.urls()  # the homepage was checked
    assert routes.get("a.dk").verified_at.startswith("2026-11-01")  # type: ignore[union-attr]


def test_lookup_job_by_company_and_job_id(tmp_path: Path) -> None:
    web = FakeWeb()
    site(web, "a.dk")
    s, _ = scraper(web, tmp_path)
    # Never seen before: lookup finds the company's route first, then the job.
    lookup = s.lookup_job("a.dk", "https://a.dk/karriere/kok-1")
    assert lookup.listing_status is ListingStatus.LISTED and lookup.job and lookup.job.details.title == "Kok"
    del web.routes["https://a.dk/karriere/kok-1"]
    assert s.lookup_job("a.dk", "https://a.dk/karriere/kok-1").listing_status is ListingStatus.NOT_LISTED
    assert s.lookup_job("nothing.dk", "x").listing_status is ListingStatus.CHECK_FAILED


def test_scrape_many(tmp_path: Path) -> None:
    web = FakeWeb()
    site(web, "a.dk")
    site(web, "b.dk")
    s, _ = scraper(web, tmp_path)
    seen: list[str] = []
    results = s.scrape_many(
        [ScrapeRequest("a.dk"), ScrapeRequest("b.dk")], workers=2, on_result=lambda r: seen.append(r.domain)
    )
    assert sorted(seen) == ["a.dk", "b.dk"] and all(len(r.jobs) == 1 for r in results)


def test_every_scrape_reports_its_usage_and_request_id(tmp_path: Path) -> None:
    web = FakeWeb()
    site(web, "a.dk")
    s, _ = scraper(web, tmp_path)
    first = s.scrape("a.dk")
    assert first.request_id.startswith("req_")
    # robots.txt, homepage, slug guess, careers page and the job page are all counted.
    assert first.usage.http.requests == len(web.requests) and first.usage.http.bytes > 0
    assert first.usage.jev.calls == 0 and first.usage.jev.usd == 0  # no Jev key in this test
    web.requests.clear()
    again = s.scrape("a.dk", known_job_ids=["https://a.dk/karriere/kok-1"])
    assert again.usage.http.requests == len(web.requests) < first.usage.http.requests  # cached is cheaper
    assert again.request_id != first.request_id


def test_a_budget_stops_the_scrape_but_keeps_what_was_paid_for(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://a.dk/", f"<a href='/karriere'>Karriere</a>{FILLER}")
    links = "".join(f"<a href='/karriere/kok-{i}'>Kok {i}</a>" for i in range(5))
    web.html("https://a.dk/karriere", f"{links}{FILLER}")
    for i in range(5):
        web.html(f"https://a.dk/karriere/kok-{i}", f"<h1>Kok {i}</h1><p>{'Mad. ' * 100}</p>")
    s, routes = scraper(web, tmp_path)
    full = s.scrape("b.dk")  # nothing there; just to count a baseline is not needed
    assert full.status is Status.FAILED

    capped = s.scrape("a.dk", budget=Budget(max_http_requests=6))
    assert capped.problem is Problem.BUDGET_EXCEEDED and capped.status is Status.PAGE
    assert 0 < len(capped.jobs) < 5 and capped.usage.http.requests == 6
    assert routes.get("a.dk").route.careers_url == "https://a.dk/karriere"  # type: ignore[union-attr]

    tiny = s.scrape("c.dk", budget=Budget(max_http_requests=0))
    assert tiny.status is Status.FAILED and tiny.problem is Problem.BUDGET_EXCEEDED
    assert tiny.usage.http.requests == 0
