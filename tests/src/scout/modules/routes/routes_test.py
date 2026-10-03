"""Tests for RouteCache: what is kept about a route, and what a failed scrape may not erase."""

from __future__ import annotations

from datetime import UTC, datetime

from scout.modules.results import BoardRef, FoundBy, Job, Problem, ScrapeResult, Status
from scout.modules.routes import RouteCache

DAY1 = datetime(2026, 10, 1, tzinfo=UTC)
DAY2 = datetime(2026, 10, 2, tzinfo=UTC)


def board(n_jobs: int = 2) -> ScrapeResult:
    return ScrapeResult(
        "acme.dk",
        Status.BOARD,
        "https://acme.teamtailor.com/jobs",
        BoardRef("teamtailor", "acme"),
        FoundBy.HOMEPAGE_LINK,
        jobs=tuple(Job(str(i), "u", None) for i in range(n_jobs)),
        link_verdicts=(("https://acme.dk/x", 0.1),),
    )


def test_keeps_the_route_not_the_jobs() -> None:
    cache = RouteCache(":memory:")
    cache.save(board(), DAY1, verified=True)
    cached = cache.get("acme.dk")
    assert cached is not None
    assert cached.route.board == BoardRef("teamtailor", "acme") and cached.route.jobs == ()
    assert cached.job_count == 2 and cached.verified_at.startswith("2026-10-01")  # type: ignore[union-attr]
    assert cache.link_verdicts("acme.dk") == {"https://acme.dk/x": 0.1}


def test_verified_at_moves_only_when_verified() -> None:
    cache = RouteCache(":memory:")
    cache.save(board(), DAY1, verified=True)
    cache.save(board(3), DAY2, verified=False)
    cached = cache.get("acme.dk")
    assert cached.verified_at.startswith("2026-10-01") and cached.scraped_at.startswith("2026-10-02")  # type: ignore[union-attr]
    assert cached.job_count == 3  # type: ignore[union-attr]


def test_a_failed_scrape_keeps_the_last_good_route() -> None:
    cache = RouteCache(":memory:")
    cache.save(board(), DAY1, verified=True)
    cache.save(
        ScrapeResult("acme.dk", Status.FAILED, problem=Problem.UNREACHABLE, reason="timeout"),
        DAY2,
        verified=False,
    )
    cached = cache.get("acme.dk")
    assert cached is not None
    assert cached.route.status is Status.BOARD and cached.route.problem is Problem.UNREACHABLE
    assert cached.route.board == BoardRef("teamtailor", "acme")  # still followable next time
    assert cached.last_ok_at.startswith("2026-10-01")  # type: ignore[union-attr]


def test_a_first_scrape_that_fails_is_recorded_as_failed() -> None:
    cache = RouteCache(":memory:")
    cache.save(ScrapeResult("new.dk", Status.FAILED, problem=Problem.BLOCKED), DAY1, verified=False)
    assert cache.get("new.dk").route.status is Status.FAILED  # type: ignore[union-attr]


def test_the_render_flag_is_kept_and_old_databases_are_migrated(tmp_path) -> None:
    import sqlite3
    from dataclasses import replace

    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)  # a database from before the careers_rendered column
    db.execute(
        "CREATE TABLE routes (domain TEXT PRIMARY KEY, status TEXT NOT NULL, problem TEXT NOT NULL DEFAULT '', "
        "reason TEXT NOT NULL DEFAULT '', careers_url TEXT NOT NULL DEFAULT '', board_system TEXT NOT NULL DEFAULT '', "
        "board_slug TEXT NOT NULL DEFAULT '', board_extra TEXT NOT NULL DEFAULT '', found_by TEXT NOT NULL DEFAULT '', "
        "job_count INTEGER NOT NULL DEFAULT 0, scraped_at TEXT NOT NULL, last_ok_at TEXT, verified_at TEXT)"
    )
    db.execute("INSERT INTO routes (domain, status, scraped_at) VALUES ('old.dk', 'page', '2026-10-01')")
    db.commit()
    db.close()
    cache = RouteCache(path)
    assert cache.get("old.dk").route.careers_rendered is False  # type: ignore[union-attr]
    page = ScrapeResult("acme.dk", Status.PAGE, "https://acme.dk/karriere", careers_rendered=True)
    cache.save(page, DAY1, verified=True)
    assert cache.get("acme.dk").route.careers_rendered is True  # type: ignore[union-attr]
    cache.save(replace(page, careers_rendered=False), DAY2, verified=False)
    assert cache.get("acme.dk").route.careers_rendered is False  # type: ignore[union-attr]
