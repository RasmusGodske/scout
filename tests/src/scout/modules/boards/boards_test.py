"""Tests for the boards module's public surface: Boards.fetch / fetch_job / guess, detect_boards,
slug_candidates, name_matches. Feeds are real vendor responses saved on 2026-10-03 (trimmed to
two jobs, personal data scrubbed) and served through a fake web."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scout.modules.boards import (
    BoardError,
    Boards,
    BoardsConfig,
    detect_boards,
    name_matches,
    slug_candidates,
)
from scout.modules.fetch import Fetcher, FetchPolicy
from scout.modules.results import BoardRef, Salary
from support.sites import FakeWeb

SUPPORT = Path(__file__).parent / "support"


def read(name: str) -> str:
    return (SUPPORT / name).read_text(encoding="utf-8")


def boards_on(web: FakeWeb, **config: object) -> Boards:
    fetcher = Fetcher(
        FetchPolicy(user_agent="test", min_interval_seconds=0, retries=0), transport=web.transport()
    )
    return Boards(fetcher, BoardsConfig(**config))  # type: ignore[arg-type]


# --- reading each system's feed -------------------------------------------------------------


def test_greenhouse_reads_jobs_with_unescaped_text() -> None:
    web = FakeWeb()
    web.text(
        "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true", read("greenhouse_jobs.json")
    )
    listing = boards_on(web).fetch(BoardRef("greenhouse", "airbnb"))
    assert listing is not None
    assert listing.company_name == "Airbnb"
    job = listing.jobs[0]
    assert job.id.isdigit() and job.details.title and job.url.startswith("https://")
    assert job.content is not None
    assert job.content.format == "json" and json.loads(job.content.raw)["id"] == int(job.id)
    assert job.content.text and "&lt;" not in job.content.text and "<p>" not in job.content.text


def test_unknown_board_is_none_not_an_error() -> None:
    web = FakeWeb()  # every URL answers 404
    assert boards_on(web).fetch(BoardRef("greenhouse", "nope")) is None
    assert boards_on(web).fetch(BoardRef("lever", "nope")) is None


def test_a_failing_feed_is_a_board_error() -> None:
    web = FakeWeb()
    web.text("https://api.ashbyhq.com/posting-api/job-board/acme", "oops", status=500)
    with pytest.raises(BoardError):
        boards_on(web).fetch(BoardRef("ashby", "acme"))


def test_lever_ashby_recruitee_hibob_read_titles_and_text() -> None:
    web = FakeWeb()
    web.text("https://api.lever.co/v0/postings/spotify?mode=json", read("lever_postings.json"))
    web.text("https://api.ashbyhq.com/posting-api/job-board/pleo", read("ashby_job_board.json"))
    web.text("https://channable.recruitee.com/api/offers", read("recruitee_offers.json"))
    web.text("https://dixa.careers.hibob.com/api/job-ad", read("hibob_job_ad.json"))
    boards = boards_on(web)
    for ref in (
        BoardRef("lever", "spotify"),
        BoardRef("ashby", "pleo"),
        BoardRef("recruitee", "channable"),
        BoardRef("hibob", "dixa"),
    ):
        listing = boards.fetch(ref)
        assert listing is not None and listing.jobs, ref
        assert all(
            j.details.title and j.url.startswith("https://") and j.content.text for j in listing.jobs
        ), ref
        assert all(
            j.details.posted_at and j.details_sources["posted_at"] == "board_feed" for j in listing.jobs
        ), ref
    assert not boards.fetch(BoardRef("lever", "spotify")).identifies_company  # type: ignore[union-attr]


def test_recruitee_drops_the_mailbox_address() -> None:
    web = FakeWeb()
    web.text("https://channable.recruitee.com/api/offers", read("recruitee_offers.json"))
    listing = boards_on(web).fetch(BoardRef("recruitee", "channable"))
    assert listing is not None
    assert all("mailbox_email" not in json.loads(j.content.raw) for j in listing.jobs)


def test_teamtailor_reads_rss_on_its_own_and_a_custom_host() -> None:
    web = FakeWeb()
    web.text(
        "https://lunar.teamtailor.com/jobs.rss",
        read("teamtailor_jobs.rss"),
        content_type="application/rss+xml",
    )
    web.text(
        "https://jobs.lunar.app/jobs.rss", read("teamtailor_jobs.rss"), content_type="application/rss+xml"
    )
    for slug in ("lunar", "jobs.lunar.app"):
        listing = boards_on(web).fetch(BoardRef("teamtailor", slug))
        assert listing is not None and listing.company_name == "Lunar"
        job = listing.jobs[0]
        assert len(listing.jobs) == 2 and job.content.format == "xml" and job.content.text
        assert job.details.location == "København" and job.details.department == "Finance"
        assert job.details.posted_at == "2026-10-01T13:32:55Z"  # RSS pubDate, normalized to UTC


def test_teamtailor_html_404_page_is_no_board() -> None:
    web = FakeWeb()
    web.html("https://acme.teamtailor.com/jobs.rss", "<!DOCTYPE html><html>not found</html>", status=404)
    assert boards_on(web).fetch(BoardRef("teamtailor", "acme")) is None


def test_hrmanager_drops_people_and_pages_through() -> None:
    page = json.loads(read("hrmanager_positionlist.json"))
    page["PositionCountCustomer"] = 2
    web = FakeWeb()
    web.json(
        "https://api.hr-manager.net/jobportal.svc/regionsyddanmark/positionlist/json/?incAds=1&take=100&skip=0",
        page,
    )
    listing = boards_on(web).fetch(BoardRef("hrmanager", "regionsyddanmark"))
    assert listing is not None and listing.company_name == "Region Syddanmark"
    assert len(listing.jobs) == 2
    for job in listing.jobs:
        raw = json.loads(job.content.raw)
        assert not [k for k in raw if k.startswith(("ProjectLeader", "Users", "ProjectParticipants"))]
        assert job.content.text and job.details.posted_at


def test_hrmanager_unknown_alias_is_none() -> None:
    web = FakeWeb()
    web.json(
        "https://api.hr-manager.net/jobportal.svc/nope/positionlist/json/?incAds=1&take=100&skip=0",
        {"CustomerAlias": None, "Items": []},
        status=400,
    )
    assert boards_on(web).fetch(BoardRef("hrmanager", "nope")) is None


def test_emply_finds_the_widget_section_then_posts_for_jobs() -> None:
    web = FakeWeb()
    web.html("https://molslinjen.career.emply.com/ledige-stillinger", read("emply_jobs_page.html"))
    posted: list[dict] = []

    def get_page(request):  # noqa: ANN001
        posted.append(json.loads(request.content))
        import httpx

        return httpx.Response(200, text=read("emply_get_page.json"))

    web.handler("https://molslinjen.career.emply.com/api/integration/vacancy/get-page", get_page)
    listing = boards_on(web).fetch(BoardRef("emply", "molslinjen"))
    assert listing is not None and listing.jobs
    assert posted[0]["sectionId"] == "863cd402-77f8-4e2f-bcd0-fd75b425962d"
    assert listing.ref.extra == "863cd402-77f8-4e2f-bcd0-fd75b425962d"  # remembered for next time
    assert listing.jobs[0].url.startswith("https://molslinjen.career.emply.com/da/ad/")


def test_join_reads_company_page_then_each_job_and_drops_the_contact() -> None:
    web = FakeWeb()
    web.html("https://join.com/companies/poweredbysous", read("join_company_page.html"))
    web.html("https://join.com/companies/poweredbysous?page=2", read("join_company_page.html"))
    web.handler(
        "https://join.com/companies/poweredbysous/16747336-global-head-of-sales",
        lambda r: __import__("httpx").Response(200, text=read("join_job_page.html")),
    )
    listing = boards_on(web).fetch(BoardRef("join", "poweredbysous"))
    assert listing is not None
    assert listing.company_site == "https://poweredbysous.com"
    head_of_sales = [j for j in listing.jobs if j.id == "16747336"]
    assert head_of_sales and "Uncapped" in head_of_sales[0].content.text
    assert "contactName" not in head_of_sales[0].content.raw
    # The company hid its salary on JOIN (settings.showSalary false), so scout does too.
    assert head_of_sales[0].details.salary is None


# --- one job by id --------------------------------------------------------------------------


def test_fetch_job_uses_the_single_job_endpoint_and_404_means_gone() -> None:
    gh = json.loads(read("greenhouse_jobs.json"))["jobs"][0]
    web = FakeWeb()
    web.json(f"https://boards-api.greenhouse.io/v1/boards/airbnb/jobs/{gh['id']}", gh)
    boards = boards_on(web)
    job = boards.fetch_job(BoardRef("greenhouse", "airbnb"), str(gh["id"]))
    assert job is not None and job.details.title == gh["title"].strip()
    assert boards.fetch_job(BoardRef("greenhouse", "airbnb"), "1") is None


def test_fetch_job_without_endpoint_picks_from_the_list() -> None:
    web = FakeWeb()
    web.text("https://api.ashbyhq.com/posting-api/job-board/pleo", read("ashby_job_board.json"))
    first = json.loads(read("ashby_job_board.json"))["jobs"][0]["id"]
    boards = boards_on(web)
    assert boards.fetch_job(BoardRef("ashby", "pleo"), first) is not None
    assert boards.fetch_job(BoardRef("ashby", "pleo"), "not-there") is None


# --- guessing -------------------------------------------------------------------------------


def test_guess_prefers_a_board_that_names_the_company() -> None:
    web = FakeWeb()
    web.text("https://api.ashbyhq.com/posting-api/job-board/lunar", read("ashby_job_board.json"))
    web.text("https://lunar.teamtailor.com/jobs.rss", read("teamtailor_jobs.rss"))
    listing = boards_on(web, guess_systems=["ashby", "teamtailor"], slug_suffixes=[]).guess("lunar.app")
    assert listing is not None and listing.ref == BoardRef("teamtailor", "lunar")


def test_guess_rejects_a_board_named_for_another_company() -> None:
    web = FakeWeb()
    web.text(
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true", read("greenhouse_jobs.json")
    )
    assert boards_on(web, guess_systems=["greenhouse"], slug_suffixes=[]).guess("acme.dk") is None


def test_guess_returns_an_anonymous_board_as_a_lead_only() -> None:
    web = FakeWeb()
    web.text("https://api.ashbyhq.com/posting-api/job-board/pleo", read("ashby_job_board.json"))
    listing = boards_on(web, guess_systems=["ashby"], slug_suffixes=[]).guess("pleo.io")
    assert listing is not None and not listing.identifies_company


def test_slug_candidates_and_name_matches() -> None:
    assert slug_candidates("www.acme-group.dk", ["aps"])[:4] == [
        "acme-group",
        "acmegroup",
        "acme-group-dk",
        "acmegroupaps",
    ]
    assert "molslinjen" in slug_candidates("molslinjen.dk", [])
    assert "ae" not in "".join(slug_candidates("søstrene.dk", []))  # ascii-fied, not mangled
    assert name_matches("lunar.app", "Lunar")
    assert name_matches("regionsyddanmark.dk", "Region Syddanmark")
    assert name_matches("acme.dk", "Acme ApS")
    assert not name_matches("acme.dk", "Airbnb")


# --- detection ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        (
            '<a href="https://boards.greenhouse.io/acme/jobs/1">x</a>',
            "https://acme.dk/careers",
            BoardRef("greenhouse", "acme"),
        ),
        (
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
            "https://acme.dk/",
            BoardRef("greenhouse", "acme"),
        ),
        (
            '<a href="https://job-boards.eu.greenhouse.io/acme">x</a>',
            "https://acme.dk/",
            BoardRef("greenhouse", "acme", "eu"),
        ),
        ('<a href="https://jobs.lever.co/acme/123">x</a>', "https://acme.dk/", BoardRef("lever", "acme")),
        (
            '<iframe src="https://jobs.ashbyhq.com/acme"></iframe>',
            "https://acme.dk/",
            BoardRef("ashby", "acme"),
        ),
        (
            '<a href="https://apply.workable.com/acme/j/ABC/">x</a>',
            "https://acme.dk/",
            BoardRef("workable", "acme"),
        ),
        ("", "https://acme.teamtailor.com/jobs", BoardRef("teamtailor", "acme")),
        (
            '<a href="https://acme.recruitee.com/o/sales">x</a>',
            "https://acme.dk/",
            BoardRef("recruitee", "acme"),
        ),
        (
            '<a href="https://molslinjen.career.emply.com/da/forside">Job</a>',
            "https://molslinjen.dk/",
            BoardRef("emply", "molslinjen"),
        ),
        (
            '<a href="https://join.com/companies/eatsous/1-x">Apply</a>',
            "https://x.com/",
            BoardRef("join", "eatsous"),
        ),
        ("", "https://dixa.careers.hibob.com/jobs", BoardRef("hibob", "dixa")),
    ],
)
def test_detect_boards(html: str, url: str, expected: BoardRef) -> None:
    assert detect_boards(html, url)[0] == expected


def test_detect_teamtailor_and_emply_on_custom_hosts() -> None:
    teamtailor = "".join('<img src="https://images.teamtailor-cdn.com/x.png">' for _ in range(3))
    assert detect_boards(teamtailor, "https://jobs.lunar.app/jobs")[0] == BoardRef(
        "teamtailor", "jobs.lunar.app"
    )
    emply = read("emply_jobs_page.html")
    assert detect_boards(emply, "https://job.acme.dk/ledige-stillinger")[0] == BoardRef(
        "emply", "job.acme.dk", "863cd402-77f8-4e2f-bcd0-fd75b425962d"
    )


def test_detect_boards_ignores_plain_pages() -> None:
    assert detect_boards('<a href="https://www.linkedin.com/company/acme">in</a>', "https://acme.dk/") == []


def test_workable_reads_the_widget_feed() -> None:
    # Hand-built in the shape the live feed had on 2026-10-03 (the live API was rate-limiting
    # us when fixtures were saved); replace with a saved response when convenient.
    payload = {
        "name": "Hugging Face",
        "jobs": [
            {
                "title": "ML Engineer",
                "shortcode": "ABC123",
                "url": "https://apply.workable.com/j/ABC123",
                "city": "Paris",
                "country": "France",
                "description": "<p>Build things.</p>",
            }
        ],
    }
    web = FakeWeb()
    web.json("https://apply.workable.com/api/v1/widget/accounts/huggingface?details=true", payload)
    listing = boards_on(web).fetch(BoardRef("workable", "huggingface"))
    assert listing is not None and listing.company_name == "Hugging Face"
    job = listing.jobs[0]
    assert (job.id, job.details.location, job.content.text) == ("ABC123", "Paris, France", "Build things.")


def test_recruitee_salary_and_greenhouse_department_come_from_the_feed() -> None:
    web = FakeWeb()
    web.text("https://channable.recruitee.com/api/offers", read("recruitee_offers.json"))
    web.text(
        "https://boards-api.greenhouse.io/v1/boards/airbnb/jobs?content=true", read("greenhouse_jobs.json")
    )
    boards = boards_on(web)
    offer = boards.fetch(BoardRef("recruitee", "channable")).jobs[0]  # type: ignore[union-attr]
    assert offer.details.salary == Salary(3750.0, 5250.0, "EUR", "month")
    assert offer.details_sources["salary"] == "board_feed"
    gh = boards.fetch(BoardRef("greenhouse", "airbnb")).jobs[0]  # type: ignore[union-attr]
    assert gh.details.department and gh.details.posted_at.endswith("Z")  # type: ignore[union-attr]
    assert "salary" not in gh.details_sources  # Greenhouse states none: absent, not guessed
