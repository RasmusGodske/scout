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
    careers_words=["career", "careers", "jobs", "job", "karriere", "ledige stillinger", "open positions"],
    common_paths=["/careers", "/karriere"],
    unsupported_job_sites=["linkedin.com/company/*/jobs*"],
)
FILLER = "<a href=/n1>a</a><a href=/n2>b</a><a href=/n3>c</a><a href=/n4>d</a><a href=/n5>e</a>" + "x" * 600


def jev_judging(
    link_answer: dict[str, float] | None = None, choose: str | None = None, seen: list | None = None
):
    """A fake Jev. Job-link nouls by URL substring (default 0.05); job-page nouls yes unless the
    page says the job is gone or is a profile; the careers choice by URL substring, else the
    first option (keyword matches come first)."""

    def answer(q: dict[str, Any]) -> dict[str, Any]:
        if q["type"] == "noul":
            instructions = q["instructions"]
            if "page" in instructions:
                page = instructions["page"]
                gone = any(w in (page["title"] + page["text"]).lower() for w in ("not found", "profile"))
                return {"type": "noul", "noul": 0.03 if gone else 0.95}
            url = instructions["link"]["url"]
            p = next((v for k, v in (link_answer or {}).items() if k in url), 0.05)
            return {"type": "noul", "noul": p}
        options = q["criteria"]
        links = [k for k, v in options.items() if isinstance(v, dict)]
        if choose is None:
            pick = links[0] if links else "none"
        else:
            pick = next((k for k in links if choose in options[k]["url"]), "none")
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
    link_requests = [b for b in seen if any("link" in _instr(q) for q in b["questions"].values())]
    assert len(link_requests) == 1  # every link judged in one batched request
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
    assert r.board == BoardRef("greenhouse", "acme") and r.found_by is FoundBy.HOMEPAGE_LINK

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


def test_a_careers_page_that_sends_people_to_linkedin_says_so(tmp_path: Path) -> None:
    # Like cyberpilot.io: the careers page's only way to the jobs is LinkedIn's company jobs page.
    web = FakeWeb()
    web.html("https://acme.dk/", f"<a href='/job-and-career'>Job and career</a>{FILLER}")
    web.html(
        "https://acme.dk/job-and-career",
        "<a href='https://www.linkedin.com/company/acme'>in</a>"
        f"<a href='https://www.linkedin.com/company/acme/jobs/' class=btn>See all open positions</a>{FILLER}",
    )
    seen: list = []
    r = resolver(web, jev_judging(seen=seen), tmp_path).resolve("acme.dk")
    assert r.status is Status.PAGE and r.problem is Problem.JOBS_ON_UNSUPPORTED_SITE
    assert r.job_list_url == "https://www.linkedin.com/company/acme/jobs/"
    assert "linkedin.com/company/acme/jobs" in r.reason and "See all open positions" in r.reason
    assert r.careers_url == "https://acme.dk/job-and-career" and not r.jobs
    assert not any("linkedin.com" in url for url in web.urls()), "scout never fetches LinkedIn"
    assert not any("greenhouse" in url or "ashby" in url for url in web.urls()), "no board guessing"


def test_a_page_with_its_own_jobs_and_a_linkedin_link_keeps_its_jobs(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"sales-rep": "Sales rep"})
    web.html(
        "https://acme.dk/karriere",
        "<a href='/karriere/sales-rep'>Sales rep</a>"
        f"<a href='https://www.linkedin.com/company/acme/jobs/'>Also on LinkedIn</a>{FILLER}",
    )
    r = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert r.problem is None and len(r.jobs) == 1 and r.job_list_url == ""


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
    asked = [
        _instr(q)["link"]["url"] for body in seen for q in body["questions"].values() if "link" in _instr(q)
    ]
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


# --- precision ------------------------------------------------------------------------------


def test_a_lone_keyword_link_is_still_checked_by_jev(tmp_path: Path) -> None:
    # "Job" is a person's name here; the real careers page is linked as "Werken bij".
    web = FakeWeb()
    web.html(
        "https://acme.nl/",
        f"<a href='/team/job-van-den-berk'>Job van den Berk</a><a href='/werken-bij'>Werken bij</a>{FILLER}",
    )
    web.html("https://acme.nl/werken-bij", f"<a href='/werken-bij/kok'>Kok</a>{FILLER}")
    web.html("https://acme.nl/werken-bij/kok", f"<h1>Kok</h1><p>{'Koken. ' * 100}</p>")
    r = resolver(web, jev_judging({"/werken-bij/": 0.95}, choose="werken-bij"), tmp_path).resolve("acme.nl")
    assert r.careers_url == "https://acme.nl/werken-bij" and [j.details.title for j in r.jobs] == ["Kok"]


def test_job_pages_that_are_not_open_ads_are_dropped(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef", "gammel": "Job not found"})
    seen: list[dict] = []
    r = resolver(web, jev_judging({"/karriere/": 0.95}, seen=seen), tmp_path).resolve("acme.dk")
    assert [j.details.title for j in r.jobs] == ["Salgschef"]
    page_questions = [q for body in seen for q in body["questions"].values() if "page" in _instr(q)]
    assert len(page_questions) == 2  # both fetched pages checked, in one request


def test_without_jev_a_job_gone_title_is_still_dropped(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef", "gammel": "Job not found"})
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert [j.details.title for j in r.jobs] == ["Salgschef"]


def test_lookup_of_a_page_that_now_says_job_not_found_is_not_listed(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"salgschef": "Salgschef"})
    res = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path)
    route = res.resolve("acme.dk")
    web.html("https://acme.dk/karriere/salgschef", "<h1>Job not found</h1><p>This position has closed.</p>")
    assert res.lookup_job(route, route.jobs[0].id).listing_status is ListingStatus.NOT_LISTED


def _instr(question: dict[str, Any]) -> dict[str, Any]:
    """A yes/no question's structured instructions; {} for anything else."""
    instructions = question["instructions"]
    return instructions if question["type"] == "noul" and isinstance(instructions, dict) else {}


def test_two_links_to_the_same_job_page_are_one_job(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<a href='/karriere'>Karriere</a>{FILLER}")
    web.html(
        "https://acme.dk/karriere",
        f"<a href='/karriere/kok'>Kok</a><a href='/karriere/kok-gammel'>Kok (gammelt link)</a>{FILLER}",
    )
    web.html("https://acme.dk/karriere/kok", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    web.redirect("https://acme.dk/karriere/kok-gammel", "https://acme.dk/karriere/kok")
    r = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert [j.url for j in r.jobs] == ["https://acme.dk/karriere/kok"]


def test_an_unlinked_guessed_board_loses_to_the_companys_own_careers_page(tmp_path: Path) -> None:
    # Like gefeba.de: a few ads on a board the site never links, the real list on its own page.
    web = FakeWeb()
    teamtailor_feed(web)  # names "Acme", so the guess passes the name check
    careers_site(web, {"kok": "Kok", "smed": "Smed", "elektriker": "Elektriker"})
    r = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert r.status is Status.PAGE and len(r.jobs) == 3


def test_an_unlinked_guessed_board_is_used_when_the_site_shows_no_jobs(tmp_path: Path) -> None:
    web = FakeWeb()
    teamtailor_feed(web)
    careers_site(web, {})
    r = resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert r.board == BoardRef("teamtailor", "acme") and r.found_by is FoundBy.SLUG_GUESS


def test_a_path_that_serves_the_homepage_is_not_a_careers_page(tmp_path: Path) -> None:
    # Like kaliber5.de: /karriere answers 200 with the homepage instead of a 404.
    web = FakeWeb()
    home = f"<html><head><title>Acme</title></head><body>{FILLER}</body></html>"
    web.html("https://acme.dk/", home)
    web.html("https://acme.dk/karriere", home)
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.status is Status.NO_CAREERS_PAGE


def test_the_job_list_is_followed_when_the_only_job_link_is_not_a_job(tmp_path: Path) -> None:
    # Like inray.de: the landing page has one link that looks like a job but is not an open ad,
    # and a link to the real list on a hiring system.
    web = FakeWeb()
    teamtailor_feed(web, slug="acme")
    web.html("https://acme.dk/", f"<a href='/karriere'>Karriere</a>{FILLER}")
    web.html(
        "https://acme.dk/karriere",
        f"<a href='/karriere/profile-anna'>Anna</a><a href='https://acme.teamtailor.com/jobs'>Jobs</a>{FILLER}",
    )
    web.html("https://acme.dk/karriere/profile-anna", "<h1>Anna, profile</h1><p>Our colleague Anna.</p>")
    web.html("https://acme.teamtailor.com/jobs", "<p>jobs</p>" + "x" * 600)
    r = resolver(web, jev_judging({"/karriere/": 0.95}, choose="karriere"), tmp_path).resolve("acme.dk")
    assert r.board == BoardRef("teamtailor", "acme") and r.jobs


# --- step 2: cheaper discovery ----------------------------------------------------------------


def test_a_careers_page_is_found_in_the_sitemap(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<p>{FILLER}</p>")
    web.text(
        "https://acme.dk/sitemap.xml",
        '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://acme.dk/om-os</loc></url><url><loc>https://acme.dk/jobs-at-acme</loc></url>"
        "</urlset>",
        content_type="application/xml",
    )
    web.html("https://acme.dk/jobs-at-acme", f"<a href='/jobs-at-acme/kok'>Kok</a>{FILLER}")
    web.html("https://acme.dk/jobs-at-acme/kok", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.found_by is FoundBy.SITEMAP and r.careers_url == "https://acme.dk/jobs-at-acme"
    assert "https://acme.dk/careers" not in web.urls()  # no common paths tried


def test_no_guessing_when_the_site_shows_its_jobs(tmp_path: Path) -> None:
    web = FakeWeb()
    careers_site(web, {"kok": "Kok"})
    resolver(web, jev_judging({"/karriere/": 0.95}), tmp_path).resolve("acme.dk")
    assert not [u for u in web.urls() if "greenhouse" in u or "ashby" in u or "teamtailor" in u]


def test_guessing_is_the_last_resort_and_must_name_the_company(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<p>{FILLER}</p>")  # no careers page anywhere
    web.text(
        "https://api.ashbyhq.com/posting-api/job-board/acme",
        (BOARDS_SUPPORT / "ashby_job_board.json").read_text(),
    )
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.status is Status.NO_CAREERS_PAGE  # Ashby names nobody: not trusted
    teamtailor_feed(web)  # names Acme
    r = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert r.board == BoardRef("teamtailor", "acme") and r.found_by is FoundBy.SLUG_GUESS


def test_no_careers_page_is_answered_from_the_cache_until_stale(tmp_path: Path) -> None:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<p>{FILLER}</p>")
    first = resolver(web, tmp=tmp_path).resolve("acme.dk")
    assert first.status is Status.NO_CAREERS_PAGE
    web.requests.clear()
    cached = resolver(web, tmp=tmp_path).resolve("acme.dk", Known(previous=replace_jobs(first)))
    assert cached.status is Status.NO_CAREERS_PAGE and cached.route_source is RouteSource.CACHED
    assert web.urls() == []  # nothing fetched
    resolver(web, tmp=tmp_path).resolve("acme.dk", Known(previous=replace_jobs(first), route_stale=True))
    assert "https://acme.dk/" in web.urls()  # re-checked once stale
