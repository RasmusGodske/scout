"""Tests for the pages module: pure functions over saved real pages (2026-10-03)."""

from __future__ import annotations

from pathlib import Path

from scout.modules.pages import (
    Link,
    canonical_url,
    careers_candidates,
    collect_links,
    extract_text,
    job_link_candidates,
    job_link_heuristic,
    page_title,
    unsupported_job_site_links,
)

SUPPORT = Path(__file__).parent / "support"
WORDS = ["career", "careers", "jobs", "job", "karriere", "ledige stillinger", "join us"]


def read(name: str) -> str:
    return (SUPPORT / name).read_text(encoding="utf-8")


def test_collect_links_resolves_and_filters() -> None:
    html = """<base href="https://acme.dk/da/">
    <a href="karriere">Karriere</a><a href="#top">top</a><a href="mailto:x@y.dk">mail</a>
    <a href="/file.pdf">pdf</a><a href="https://acme.dk/da/karriere#x">Karriere og job</a>
    <a href="/om" aria-label="Om os"></a>"""
    links = collect_links(html, "https://acme.dk/")
    assert links == [
        Link("https://acme.dk/da/karriere", "Karriere og job"),
        Link("https://acme.dk/om", "Om os"),
    ]


def test_careers_candidates_merge_language_variants_and_skip_social() -> None:
    links = [
        Link("https://acme.dk/da/karriere", "Karriere"),
        Link("https://acme.dk/en/karriere", "Careers"),
        Link("https://www.linkedin.com/company/acme/jobs", "Jobs"),
        Link("https://acme.dk/om", "Om os"),
    ]
    assert [l.url for l in careers_candidates(links, WORDS)] == ["https://acme.dk/da/karriere"]


def test_careers_word_must_be_a_word() -> None:
    # "job" inside "jobbing" or a host like "blowjobs" isn't a careers link; whole words are.
    assert careers_candidates([Link("https://acme.dk/jobbing-tips", "tips")], ["job"]) == []
    assert careers_candidates([Link("https://acme.dk/x", "Ledige stillinger")], WORDS)


def test_real_homepage_points_at_the_emply_site() -> None:
    links = collect_links(read("molslinjen_homepage.html"), "https://www.molslinjen.dk/")
    urls = [l.url for l in careers_candidates(links, WORDS)]
    assert any("molslinjen.career.emply.com" in u for u in urls)


def test_job_link_candidates_on_a_real_careers_page() -> None:
    page = "https://careers.siteimprove.com/open-positions/"
    # Saved after rendering: the plain page has no job links, its list is injected by script.
    links = collect_links(read("siteimprove_careers_rendered.html"), page)
    candidates = job_link_candidates(links, page, 500)
    positions = [l for l in candidates if "/position/?id=" in l.url or "/position?id=" in l.url]
    assert len(positions) >= 5
    assert all(not l.url.rstrip("/").endswith("open-positions") for l in candidates)
    assert all("linkedin" not in l.url for l in candidates)


def test_job_ads_on_hiring_systems_with_their_own_url_shapes_are_candidates() -> None:
    # Shapes seen on production misses (company names replaced): Talentadore's /apply/<slug>/<id>,
    # Reqruiting's job.aspx?id=<n>, a Swedish recruiter's /tjanst/<id>-<slug>. Before, off-site
    # links in these shapes were dropped before Jev ever saw them.
    page = "https://acme.com/open-positions/"
    html = """<main>
      <a href="https://ats.talentadore.com/apply/ai-engineer/DokV4P">AI Engineer</a>
      <a href="https://AcmeAS.reqruiting.com/job.aspx?id=28792">Apply for Consultant</a>
      <a href="https://www.recruiter.se/tjanst/3472-produktionschef-till-acme">via länken</a>
      <a href="https://partner.example.com/products/widget">Our partner</a>
      <a href="https://AcmeAS.reqruiting.com/Pages/Application/Login.aspx?customerId=3672">Log in</a>
    </main>"""
    urls = [l.url for l in job_link_candidates(collect_links(html, page), page, 500)]
    assert urls == [
        "https://ats.talentadore.com/apply/ai-engineer/DokV4P",
        "https://AcmeAS.reqruiting.com/job.aspx?id=28792",
        "https://www.recruiter.se/tjanst/3472-produktionschef-till-acme",
    ]
    assert all(job_link_heuristic(Link(u, ""), page) >= 0.6 for u in urls)
    # The Swedish plural "tjänster" means services, not positions.
    assert job_link_heuristic(Link("https://acme.se/tjanster/it-support/", "IT-support"), page) < 0.6


def test_job_link_heuristic_tells_ads_from_navigation() -> None:
    careers = "https://acme.dk/karriere"
    assert job_link_heuristic(Link("https://acme.dk/karriere/salgschef-123", "Salgschef"), careers) >= 0.6
    assert job_link_heuristic(Link("https://acme.dk/job/REF5696K-Copenhagen/", "Controller"), careers) >= 0.6
    assert job_link_heuristic(Link("https://acme.dk/om-os", "Om os"), careers) < 0.6


def test_canonical_url_is_a_stable_id() -> None:
    a = canonical_url("HTTPS://Acme.dk/job/123/?utm_source=x&b=2&a=1#apply")
    b = canonical_url("https://acme.dk/job/123?a=1&b=2&gclid=zz")
    assert a == b == "https://acme.dk/job/123?a=1&b=2"


def test_extract_text_from_a_job_page_and_a_fragment() -> None:
    text = extract_text(read("netcompany_job.html"))
    assert "Business Controller" in text or "Controller" in text
    assert "<div" not in text and len(text) > 500
    assert extract_text("<p>Salgschef</p><ul><li>OTE 900k</li></ul>") == "Salgschef\n- OTE 900k"
    assert extract_text("") == ""


def test_page_title() -> None:
    assert (
        page_title("<html><head><title>T</title></head><body><h1> Head  of Sales </h1></body></html>")
        == "Head of Sales"
    )


def test_extract_text_gives_the_same_text_however_often_a_page_is_read() -> None:
    # trafilatura's de-duplication is a cache shared by the whole process: a paragraph seen more
    # than twice is dropped, so the fourth job of a company lost the footer every ad shares, and
    # an unchanged ad read again came back shorter.
    page = read("lunar_teamtailor_job.html")
    texts = [extract_text(page) for _ in range(5)]
    assert len(set(texts)) == 1


SITES = ["linkedin.com/company/*/jobs*", "linkedin.com/jobs*"]


def test_links_to_unsupported_job_sites_are_found_and_social_links_are_not() -> None:
    links = [
        Link("https://www.linkedin.com/company/acme", ""),  # the company's profile, not its jobs
        Link("https://www.linkedin.com/company/acme/jobs/", "See all open positions"),
        Link("https://dk.linkedin.com/jobs/view/123", "Account Executive"),
        Link("https://acme.dk/jobs", "Jobs"),
        Link("https://notlinkedin.com/company/acme/jobs", "Jobs"),
    ]
    found = unsupported_job_site_links(links, SITES)
    assert [link.url for link in found] == [
        "https://www.linkedin.com/company/acme/jobs/",
        "https://dk.linkedin.com/jobs/view/123",
    ]
    assert unsupported_job_site_links(links, []) == []
