"""Tests for the benchmark's judge and label reader."""

from __future__ import annotations

from pathlib import Path

from scout.modules.benchmark import Label, Report, judge, read_labels
from scout.modules.results import BoardRef, FoundBy, Job, Problem, ScrapeResult, Status


def label(**kw: object) -> Label:
    base = dict(
        domain="acme.dk",
        careers_url="",
        system="",
        slug="",
        open_jobs=None,
        labelled_by="claude",
        checked_by="",
    )
    return Label(**{**base, **kw})  # type: ignore[arg-type]


def jobs(n: int) -> tuple[Job, ...]:
    return tuple(Job(str(i), "u", None) for i in range(n))


def test_board_hit_and_misses() -> None:
    result = ScrapeResult(
        "acme.dk",
        Status.BOARD,
        board=BoardRef("teamtailor", "acme"),
        found_by=FoundBy.SLUG_GUESS,
        jobs=jobs(10),
    )
    supported = frozenset({"teamtailor", "emply"})
    assert judge(label(system="teamtailor", slug="acme", open_jobs=11), result, supported).hit
    assert not judge(label(system="emply"), result, supported).hit
    assert not judge(label(system="teamtailor", open_jobs=30), result, supported).hit  # far off the count


def test_custom_page_language_variants_are_the_same_page() -> None:
    result = ScrapeResult("acme.dk", Status.PAGE, careers_url="https://www.acme.dk/en/careers/", jobs=jobs(2))
    assert judge(label(careers_url="https://acme.dk/careers", open_jobs=2), result).hit
    assert not judge(label(careers_url="https://acme.dk/om-os"), result).hit


def test_no_careers_page_label() -> None:
    none = ScrapeResult("acme.dk", Status.NO_CAREERS_PAGE, problem=Problem.NO_CAREERS_PAGE)
    assert judge(label(system="no_careers_page"), none).hit
    assert not judge(label(system="teamtailor"), none).hit


def test_report_and_reader(tmp_path: Path) -> None:
    csv = tmp_path / "labels.csv"
    csv.write_text(
        "# comment line\ndomain,careers_url,system,slug,open_jobs,labelled_by,checked_by,notes\n"
        "acme.dk,https://acme.dk/karriere,,,3,claude,rasmus,\nb.dk,,teamtailor,b,,claude,,\n"
    )
    labels = read_labels(csv)
    assert [l.checked for l in labels] == [True, False] and labels[0].open_jobs == 3
    report = Report(
        [
            judge(
                labels[0],
                ScrapeResult(
                    "acme.dk",
                    Status.PAGE,
                    "https://acme.dk/karriere",
                    found_by=FoundBy.COMMON_PATH,
                    jobs=jobs(3),
                ),
            ),
            judge(labels[1], ScrapeResult("b.dk", Status.FAILED, problem=Problem.UNREACHABLE, reason="x")),
        ]
    )
    assert report.hit_rate == 0.5
    assert report.by_found_by() == {"-": (0, 1), "common_path": (1, 1)}
    assert report.miss_reasons() == {"failed": 1}


def test_a_careers_page_and_its_parent_on_the_same_host_match() -> None:
    result = ScrapeResult("s.com", Status.PAGE, careers_url="https://careers.s.com/", jobs=jobs(1))
    assert judge(label(careers_url="https://careers.s.com/open-positions/"), result).hit
    other_host = ScrapeResult("s.com", Status.PAGE, careers_url="https://s.com/", jobs=jobs(1))
    assert not judge(label(careers_url="https://careers.s.com/open-positions/"), other_host).hit


def page_jobs(*urls: str) -> tuple[Job, ...]:
    return tuple(Job(u, u, None) for u in urls)


def test_jobs_on_an_unsupported_hiring_system_are_judged_as_a_page() -> None:
    result = ScrapeResult(
        "a.de",
        Status.PAGE,
        careers_url="https://a.jobs.personio.de/",
        jobs=page_jobs("https://a.jobs.personio.de/job/1"),
    )
    assert judge(
        label(system="personio", careers_url="https://a.jobs.personio.de/"), result, frozenset({"teamtailor"})
    ).hit


def test_precision_and_recall_per_job() -> None:
    real = ("https://a.dk/da/job/1", "https://a.dk/job/2", "https://a.dk/job/3")
    result = ScrapeResult(
        "a.dk",
        Status.PAGE,
        careers_url="https://a.dk/karriere",
        jobs=page_jobs(
            "https://www.a.dk/en/job/1/",
            "https://a.dk/job/2?utm_source=x",
            "https://a.dk/team/job-van-den-berk",
        ),
    )
    v = judge(label(careers_url="https://a.dk/karriere", job_urls=real), result)
    assert (v.true_jobs, v.false_jobs, v.missed_jobs) == (2, 1, 1)  # language prefix and tracking ignored
    assert not v.hit and "false jobs" in v.misses[0]

    closed = ScrapeResult(
        "b.dk", Status.PAGE, careers_url="https://b.dk/careers", jobs=page_jobs("https://b.dk/job/gone")
    )
    w = judge(label(domain="b.dk", careers_url="https://b.dk/careers", open_jobs=0), closed)
    assert (w.true_jobs, w.false_jobs, w.missed_jobs) == (0, 1, 0)

    report = Report([v, w])
    assert report.job_precision() == 2 / 4 and report.job_recall() == 2 / 3


def test_an_unsupported_board_embedded_in_the_company_page_counts() -> None:
    result = ScrapeResult(
        "m.de",
        Status.PAGE,
        careers_url="https://m.de/karriere",
        jobs=page_jobs("https://m.jobs.personio.de/job/1"),
    )
    assert judge(label(system="personio", careers_url="https://m.jobs.personio.de/"), result).hit
