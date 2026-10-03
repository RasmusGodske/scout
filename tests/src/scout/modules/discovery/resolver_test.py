"""Tests for Resolver: the search order, known jobs, the cached route and its three
rediscovery rules, single-job lookup and typed problems, on a fake web with a fake Jev."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from scout.modules.boards import Boards, BoardsConfig
from scout.modules.discovery import DiscoveryConfig, Known, Resolver
from scout.modules.fetch import Fetcher, FetchPolicy
from scout.modules.jev import Jev, JevConfig
from scout.modules.results import (
    BoardRef,
    DetailSource,
    FoundBy,
    ListingStatus,
    Problem,
    RediscoveryReason,
    RouteSource,
    ScrapeResult,
    Status,
)
from support.sites import FakeWeb, jev_transport

BOARDS_SUPPORT = Path(__file__).parents[1] / "boards" / "support"
CONFIG = DiscoveryConfig(
    careers_words=["career", "careers", "jobs", "job", "karriere", "ledige stillinger"],
    common_paths=["/careers", "/karriere"],
)
FILLER = "<a href=/n1>a</a><a href=/n2>b</a><a href=/n3>c</a><a href=/n4>d</a><a href=/n5>e</a>" + "x" * 600


def jev_judging(
    link_answer: dict[str, float] | None = None, choose: str | None = None, seen: list | None = None
):
    """A fake Jev: nouls by URL substring (default 0.05), choice by URL substring."""

    def answer(q: dict[str, Any]) -> dict[str, Any]:
        if q["type"] == "noul":
            url = q["instructions"]["link"]["url"]
            p = next((v for k, v in (link_answer or {}).items() if k in url), 0.05)
            return {"type": "noul", "noul": p}
        options = q["criteria"]
        pick = next(
            (k for k, v in options.items() if isinstance(v, dict) and choose and choose in v["url"]), "none"
        )
        probs = {k: (0.9 if k == pick else 0.1 / max(1, len(options) - 1)) for k in options}
        return {"type": "choice", "choice": pick, "probabilities": probs, "confidence": 0.8}

    return jev_transport(answer, seen)


def resolver(web: FakeWeb, jev: httpx.MockTransport | None = None, tmp: Path | None = None) -> Resolver:
    fetcher = Fetcher(
        FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=web.transport()
    )
    boards = Boards(
        fetcher, BoardsConfig(guess_systems=["greenhouse", "ashby", "teamtailor"], slug_suffixes=[])
    )
    config = JevConfig(
        model="typesafe/jev-1.13-20260917", usage_log=str((tmp or Path("/tmp")) / "jev-test.jsonl")
    )
    return Resolver(fetcher, boards, Jev(config, "key" if jev else None, transport=jev), CONFIG)


def careers_site(web: FakeWeb, jobs: dict[str, str], home_link: str = "/karriere") -> None:
    links = "".join(f'<a href="/karriere/{slug}">{title}</a>' for slug, title in jobs.items())
    web.html("https://acme.dk/", f"<a href='{home_link}'>Karriere</a>{FILLER}")
    web.html("https://acme.dk/karriere", f"<h1>Job hos Acme</h1>{links}{FILLER}")
    for slug, title in jobs.items():
        web.html(f"https://acme.dk/karriere/{slug}", f"<h1>{title}</h1><p>{'Du skal sælge. ' * 40}</p>")


def teamtailor_feed(web: FakeWeb, slug: str = "acme", name: str = "Acme") -> None:
    rss = (BOARDS_SUPPORT / "teamtailor_jobs.rss").read_text().replace("Lunar", name)
    web.text(f"https://{slug}.teamtailor.com/jobs.rss", rss)


def known_route(result: ScrapeResult, *, stale: bool = False, job_ids: tuple[str, ...] = ()) -> Known:
    return Known(
        previous=replace_jobs(result),
        previous_job_count=len(result.jobs),
        route_stale=stale,
        job_ids=frozenset(job_ids),
        link_verdicts=dict(result.link_verdicts),
    )


def replace_jobs(result: ScrapeResult) -> ScrapeResult:
    from dataclasses import replace

    return replace(result, jobs=(), link_verdicts=())


# --- discovery ------------------------------------------------------------------------------


def test_custom_page_jobs_are_judged_by_jev_and_fetched(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef", "udvikler": "Udvikler"})
    seen: list[dict] = []
    r = resolver(web, jev_judging({"/karriere/": 0.95}, seen=seen), tmp_path).resolve("acme.dk")
    assert (
        r.status is Status.PAGE
        and r.found_by is FoundBy.HOMEPAGE_LINK
        and r.route_source is RouteSource.DISCOVERED
    )
    assert r.careers_url == "https://acme.dk/karriere"
    assert sorted(j.id for j in r.jobs) == [
        "https://acme.dk/karriere/salgschef",
        "https://acme.dk/karriere/udvikler",
    ]
    job = next(j for j in r.jobs if j.id.endswith("salgschef"))
    assert job.content is not None and job.content.format == "html" and "Du skal sælge" in job.content.text
    assert job.details.title == "Salgschef" and job.details_sources == {"title": DetailSource.PAGE_HEADING}
    assert job.details.posted_at is None  # the page states none
    assert len(seen) == 1  # every link judged in one batched request
    assert dict(r.link_verdicts)["https://acme.dk/karriere/salgschef"] == 0.95


def test_homepage_board_link_beats_a_stale_slug_guess(tmp_path: Path) -> None:
    web = FakeWeb()
    teamtailor_feed(web)
    gh = json.loads((BOARDS_SUPPORT / "greenhouse_jobs.json").read_text())
    for j in gh["jobs"]:
        j["company_name"] = "Acme"
    web.json("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", gh)
    web.html("https://acme.dk/", '<a href="https://boards.greenhouse.io/acme">Jobs</a>' + FILLER)
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.board == BoardRef("greenhouse", "acme") and r.found_by is FoundBy.SLUG_GUESS

    web.html("https://acme.dk/", '<a href="https://acme.teamtailor.com/jobs">Jobs</a>' + FILLER)
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.board == BoardRef("teamtailor", "acme") and r.found_by is FoundBy.HOMEPAGE_LINK


def test_an_anonymous_board_needs_the_homepage_to_confirm_it(tmp_path: Path) -> None:
    web = FakeWeb()
    web.text(
        "https://api.ashbyhq.com/posting-api/job-board/acme",
        (BOARDS_SUPPORT / "ashby_job_board.json").read_text(),
    )
    careers_site(web, {"salgschef": "Salgschef"})
    r = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert r.status is Status.PAGE  # someone else's "acme" on Ashby was not taken


def test_careers_link_without_keywords_is_chosen_by_jev(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<a href='/bliv-en-af-os'>Bliv en af os</a>{FILLER}")
    web.html("https://acme.dk/bliv-en-af-os", f"<a href='/bliv-en-af-os/kok'>Kok</a>{FILLER}")
    web.html("https://acme.dk/bliv-en-af-os/kok", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    r = resolver(web, jev_judging({"/kok": 0.9}, choose="bliv-en-af-os"), tmp_path).resolve("acme.dk")
    assert r.careers_url == "https://acme.dk/bliv-en-af-os" and [j.details.title for j in r.jobs] == ["Kok"]


def test_common_path_when_the_homepage_has_no_link(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<p>{FILLER}</p>")
    web.html("https://acme.dk/karriere", f"<a href='/karriere/kok'>Kok</a>{FILLER}")
    web.html("https://acme.dk/karriere/kok", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")  # no Jev: the URL heuristic decides
    assert r.found_by is FoundBy.COMMON_PATH and [j.details.title for j in r.jobs] == ["Kok"]


def test_typed_problems(tmp_path: Path) -> None:
    web = FakeWeb()
    web.text("https://robots.dk/robots.txt", "User-agent: *\nDisallow: /")
    for url in ("https://blocked.dk/", "https://www.blocked.dk/", "http://blocked.dk/"):
        web.html(url, "denied", status=403)
    web.html("https://empty.dk/", f"<p>{FILLER}</p>")
    careers_site(web, {})
    res = resolver(web, tmp=tmp_path)
    assert res.resolve("robots.dk").problem is Problem.ROBOTS_DISALLOWED
    assert res.resolve("blocked.dk").problem is Problem.BLOCKED
    assert res.resolve("empty.dk").problem is Problem.NO_CAREERS_PAGE
    nojobs = res.resolve("acme.dk")
    assert nojobs.status is Status.PAGE and nojobs.problem is Problem.NO_JOBS_FOUND


# --- known jobs -----------------------------------------------------------------------------


def test_known_jobs_on_a_page_are_confirmed_not_refetched(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef"})
    first = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")

    careers_site(web, {"salgschef": "Salgschef", "ny": "Ny stilling"})
    web.requests.clear()
    seen: list[dict] = []
    known = known_route(first, job_ids=("https://acme.dk/karriere/salgschef",))
    again = resolver(web, jev_judging({"/karriere/": 0.95}, seen=seen), tmp_path).resolve("acme.dk", known)
    assert again.route_source is RouteSource.CACHED
    assert "https://acme.dk/" not in web.urls()  # no rediscovery
    assert "https://acme.dk/karriere/salgschef" not in web.urls()  # known job not refetched
    old = next(j for j in again.jobs if j.id.endswith("salgschef"))
    new = next(j for j in again.jobs if j.id.endswith("/ny"))
    assert old.known and old.content is None and old.details_sources == {"title": DetailSource.LINK_TEXT}
    assert not new.known and new.content is not None
    asked = [q["instructions"]["link"]["url"] for body in seen for q in body["questions"].values()]
    assert asked == ["https://acme.dk/karriere/ny"]  # only the new link went to Jev


def test_known_jobs_on_a_board_come_back_as_references(tmp_path: Path) -> None:
    web = FakeWeb()
    teamtailor_feed(web)
    web.html("https://acme.dk/", '<a href="https://acme.teamtailor.com/jobs">Jobs</a>' + FILLER)
    first = resolver(web, tmp=tmp_path).resolve("acme.dk")
    known_id = first.jobs[0].id
    again = resolver(web, tmp=tmp_path).resolve("acme.dk", known_route(first, job_ids=(known_id,)))
    by_id = {j.id: j for j in again.jobs}
    assert by_id[known_id].known and by_id[known_id].content is None
    assert by_id[known_id].details.title  # details are free on a board, so they stay
    assert all(j.content is not None for j in again.jobs if j.id != known_id)


# --- the cached route and when it is replaced ---------------------------------------------


def test_a_broken_route_is_rediscovered(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"kok": "Kok"})
    first = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    # The site moves its jobs page; the old one now 404s.
    del web.routes["https://acme.dk/karriere"]
    web.html("https://acme.dk/", f"<a href='/jobs'>Jobs</a>{FILLER}")
    web.html("https://acme.dk/jobs", f"<a href='/jobs/kok'>Kok</a>{FILLER}")
    web.html("https://acme.dk/jobs/kok", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    again = resolver(web, jev_judging({"/jobs/": 0.95}), tmp_path).resolve("acme.dk", known_route(first))
    assert again.route_source is RouteSource.REDISCOVERED
    assert again.rediscovered_because is RediscoveryReason.ROUTE_FAILED
    assert again.careers_url == "https://acme.dk/jobs" and len(again.jobs) == 1


def test_a_route_that_suddenly_shows_nothing_is_rediscovered(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"kok": "Kok"})
    first = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    # The jobs moved to Teamtailor; the old page stays up but lists nothing.
    teamtailor_feed(web)
    web.html("https://acme.dk/", '<a href="https://acme.teamtailor.com/jobs">Jobs</a>' + FILLER)
    web.html("https://acme.dk/karriere", f"<p>See our jobs on Teamtailor</p>{FILLER}")
    again = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk", known_route(first))
    assert again.route_source is RouteSource.REDISCOVERED
    assert again.rediscovered_because is RediscoveryReason.ROUTE_EMPTY
    assert again.board == BoardRef("teamtailor", "acme") and again.jobs


def test_an_empty_route_that_discovery_confirms_is_kept(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"kok": "Kok"})
    first = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    careers_site(web, {})  # really no open jobs now
    again = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk", known_route(first))
    assert again.route_source is RouteSource.CACHED and again.problem is Problem.NO_JOBS_FOUND


def test_a_stale_route_the_homepage_still_leads_to_is_kept(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"kok": "Kok"})
    first = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    again = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve(
        "acme.dk", known_route(first, stale=True)
    )
    assert again.route_source is RouteSource.CACHED and again.route_verified


def test_a_stale_board_the_homepage_moved_away_from_is_replaced(tmp_path: Path) -> None:
    # Molslinjen's case: the old board still answers with plausible jobs, but the homepage
    # now links somewhere else.
    web = FakeWeb()
    teamtailor_feed(web)
    web.html("https://acme.dk/", '<a href="https://acme.teamtailor.com/jobs">Jobs</a>' + FILLER)
    first = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert first.board == BoardRef("teamtailor", "acme")

    careers_site(web, {"kok": "Kok"})  # the homepage now links /karriere instead
    fresh_cache = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path)
    not_stale = fresh_cache.resolve("acme.dk", known_route(first))
    assert not_stale.board == BoardRef("teamtailor", "acme")  # within 30 days: trusted as is
    stale = fresh_cache.resolve("acme.dk", known_route(first, stale=True))
    assert stale.route_source is RouteSource.REDISCOVERED
    assert stale.rediscovered_because is RediscoveryReason.ROUTE_STALE
    assert stale.status is Status.PAGE and stale.careers_url == "https://acme.dk/karriere"


# --- one job again ----------------------------------------------------------------------------


def test_lookup_job_on_a_page_listed_and_not_listed(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef"})
    res = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path)
    route = res.resolve("acme.dk")
    job_id = route.jobs[0].id
    listed = res.lookup_job(route, job_id + "/?utm_source=mail")  # as a caller might paste it
    assert (
        listed.listing_status is ListingStatus.LISTED
        and listed.job
        and listed.job.details.title == "Salgschef"
    )
    web.redirect("https://acme.dk/karriere/salgschef", "https://acme.dk/karriere")
    assert res.lookup_job(route, job_id).listing_status is ListingStatus.NOT_LISTED
    del web.routes["https://acme.dk/karriere/salgschef"]
    assert res.lookup_job(route, job_id).listing_status is ListingStatus.NOT_LISTED


def test_lookup_job_that_cannot_be_checked(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fetcher = Fetcher(
        FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=httpx.MockTransport(down)
    )
    boards = Boards(fetcher, BoardsConfig(guess_systems=[], slug_suffixes=[]))
    jev = Jev(JevConfig(model="typesafe/jev-1.13-20260917", usage_log=str(tmp_path / "u.jsonl")), None)
    res = Resolver(fetcher, boards, jev, CONFIG)
    route = ScrapeResult("acme.dk", Status.PAGE, "https://acme.dk/karriere")
    lookup = res.lookup_job(route, "https://acme.dk/karriere/kok")
    assert lookup.listing_status is ListingStatus.CHECK_FAILED and lookup.problem is Problem.UNREACHABLE
