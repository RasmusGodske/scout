"""Tests for job details stated plainly by a page: JSON-LD JobPosting, heading, link text,
and date normalization. Real pages saved 2026-10-03 plus hand-written messy cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scout.modules.pages import details_from_page, find_job_posting, normalize_date
from scout.modules.results import DetailSource, Salary

SUPPORT = Path(__file__).parent / "support"


def read(name: str) -> str:
    return (SUPPORT / name).read_text(encoding="utf-8")


def page_with(*blocks: object, body: str = "") -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{b if isinstance(b, str) else json.dumps(b)}</script>'
        for b in blocks
    )
    return f"<html><head>{scripts}</head><body>{body}</body></html>"


def test_real_teamtailor_page() -> None:
    details, sources = details_from_page(
        read("lunar_teamtailor_job.html"), "https://jobs.lunar.app/jobs/8486641"
    )
    assert details.title == "Accounts Payable/Receivable Specialist"
    assert details.posted_at == "2026-10-01T13:32:55Z"  # 15:32:55+02:00 in the page
    assert details.employment_type == "FULL_TIME"
    assert details.location == "København, DK"
    assert details.department is None  # schema.org has no such field
    assert set(sources.values()) == {DetailSource.JSON_LD}


def test_real_join_page_has_salary_in_json_ld() -> None:
    details, sources = details_from_page(read("join_job_page.html"), "https://join.com/companies/x/16747336")
    assert details.title == "Global Head of Sales" and details.posted_at == "2026-09-24T15:04:47Z"
    assert details.salary is not None and details.salary.currency == "EUR"
    assert sources["salary"] == DetailSource.JSON_LD


def test_page_without_json_ld_falls_back_to_heading_then_link_text() -> None:
    details, sources = details_from_page("<h1>Kok</h1><p>Mad</p>", "https://a.dk/job/1", "Kok (link)")
    assert (details.title, sources) == ("Kok", {"title": DetailSource.PAGE_HEADING})
    details, sources = details_from_page("<p>no heading</p>", "https://a.dk/job/1", "  Kok  ")
    assert (details.title, sources) == ("Kok", {"title": DetailSource.LINK_TEXT})
    details, sources = details_from_page("<p>nothing</p>", "https://a.dk/job/1", "")
    assert (details.title, sources) == (None, {})


@pytest.mark.parametrize(
    "block",
    [
        {"@type": "JobPosting", "title": "Sælger"},
        [{"@type": "Organization", "name": "Acme"}, {"@type": "JobPosting", "title": "Sælger"}],
        {
            "@context": "https://schema.org",
            "@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "Sælger"}],
        },
        {"@type": ["JobPosting", "Thing"], "title": "Sælger"},
    ],
)
def test_job_posting_found_in_every_shape(block: object) -> None:
    assert find_job_posting(page_with(block))["title"] == "Sælger"  # type: ignore[index]


def test_messy_fields_are_handled_by_the_standard_not_per_site() -> None:
    posting = {
        "@type": "JobPosting",
        "title": "Head of Sales &amp; Partnerships",
        "datePosted": "2026-09-01",
        "employmentType": ["FULL_TIME", "CONTRACTOR"],
        "jobLocation": [
            {
                "@type": "Place",
                "address": {"addressLocality": "Aarhus", "addressCountry": {"name": "Denmark"}},
            },
            {"@type": "Place", "address": "Copenhagen"},
        ],
        "jobLocationType": "TELECOMMUTE",
        "baseSalary": {
            "currency": "DKK",
            "value": {"minValue": "50000", "maxValue": 65000, "unitText": "MONTH"},
        },
    }
    details, _ = details_from_page(page_with(posting), "https://a.dk/j")
    assert details.title == "Head of Sales & Partnerships"
    assert details.posted_at == "2026-09-01"
    assert details.employment_type == "FULL_TIME, CONTRACTOR"
    assert details.location == "Aarhus, Denmark; Copenhagen; Remote"
    assert details.salary == Salary(50000.0, 65000.0, "DKK", "month")


def test_broken_json_ld_is_skipped_not_fatal() -> None:
    html = page_with("{ not json", {"@type": "JobPosting", "title": "Kok"})
    assert details_from_page(html, "https://a.dk/j")[0].title == "Kok"
    assert details_from_page(page_with("{ not json"), "https://a.dk/j")[0].title is None


def test_the_posting_for_this_page_wins_over_similar_jobs() -> None:
    html = page_with(
        {"@type": "JobPosting", "title": "Other job", "url": "https://a.dk/job/2"},
        {"@type": "JobPosting", "title": "This job", "url": "https://a.dk/job/1/"},
    )
    assert find_job_posting(html, "https://a.dk/job/1")["title"] == "This job"  # type: ignore[index]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-01T15:32:55+02:00", "2026-10-01T13:32:55Z"),
        ("2026-09-24T15:04:47.511Z", "2026-09-24T15:04:47Z"),
        ("2026-09-08T08:46:56.8250402Z", "2026-09-08T08:46:56Z"),  # Emply: 7 fraction digits
        ("2026-09-18 13:27:28 UTC", "2026-09-18T13:27:28Z"),  # Recruitee
        ("/Date(1790072451000+0200)/", "2026-09-22T10:20:51Z"),  # HR Manager: ms are UTC
        (1782214185805, "2026-06-23T11:29:45Z"),  # Lever, epoch milliseconds
        ("Thu, 01 Oct 2026 15:32:55 +0200", "2026-10-01T13:32:55Z"),  # RSS
        ("2026-09-01", "2026-09-01"),
        ("last week", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_date(value: object, expected: str | None) -> None:
    assert normalize_date(value) == expected
