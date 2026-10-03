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
