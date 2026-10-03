from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scout.modules.results import BoardRef, FoundBy, Problem, ScrapeResult, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS routes (
    domain         TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    problem        TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    careers_url    TEXT NOT NULL DEFAULT '',
    board_system   TEXT NOT NULL DEFAULT '',
    board_slug     TEXT NOT NULL DEFAULT '',
    board_extra    TEXT NOT NULL DEFAULT '',
    found_by       TEXT NOT NULL DEFAULT '',
    job_count      INTEGER NOT NULL DEFAULT 0,
    scraped_at     TEXT NOT NULL,
    last_ok_at     TEXT,
    verified_at    TEXT
);
CREATE TABLE IF NOT EXISTS link_verdicts (
    domain      TEXT NOT NULL REFERENCES routes(domain),
    url         TEXT NOT NULL,
    probability REAL NOT NULL,
    judged_at   TEXT NOT NULL,
    PRIMARY KEY (domain, url)
);
"""


@dataclass(frozen=True)
class CachedRoute:
    """How to read a company's jobs, as the last scrape left it.

    `route` is a ScrapeResult without jobs. Its `status` and route fields are the last good
    route; `problem` and `reason` are from the last scrape, which may have failed. `verified_at` is when discovery last found or the
    homepage last confirmed this route; `job_count` how many jobs the route gave last time.
    """

    route: ScrapeResult
    scraped_at: str
    last_ok_at: str | None
    verified_at: str | None
    job_count: int


class RouteCache:
    """SQLite cache of routes and link verdicts. `save` is the only code that writes it.

    Safe to share across threads: one connection, one lock.
    """

    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)

    def save(self, result: ScrapeResult, now: datetime, *, verified: bool) -> None:
        """Record one scrape's route. A failed scrape records only its problem: the route the
        last good scrape found stays, so the next scrape can try it again. `verified` says this
        scrape discovered the route or confirmed it on the homepage."""
        at = now.isoformat(timespec="seconds")
        with self._lock, self._db:
            if result.status is Status.FAILED:
                self._db.execute(
                    """INSERT INTO routes (domain, status, problem, reason, scraped_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(domain) DO UPDATE SET
                           status=CASE WHEN routes.last_ok_at IS NULL THEN excluded.status
                                       ELSE routes.status END,
                           problem=excluded.problem, reason=excluded.reason,
                           scraped_at=excluded.scraped_at""",
                    (result.domain, result.status.value, _problem(result), result.reason, at),
                )
                return
            board = result.board or BoardRef("", "")
            self._db.execute(
                """INSERT INTO routes (domain, status, problem, reason, careers_url, board_system,
                       board_slug, board_extra, found_by, job_count, scraped_at, last_ok_at,
                       verified_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(domain) DO UPDATE SET status=excluded.status,
                       problem=excluded.problem, reason=excluded.reason,
                       careers_url=excluded.careers_url, board_system=excluded.board_system,
                       board_slug=excluded.board_slug, board_extra=excluded.board_extra,
                       found_by=excluded.found_by, job_count=excluded.job_count,
                       scraped_at=excluded.scraped_at, last_ok_at=excluded.last_ok_at,
                       verified_at=COALESCE(excluded.verified_at, routes.verified_at)""",
                (
                    result.domain,
                    result.status.value,
                    _problem(result),
                    result.reason,
                    result.careers_url,
                    board.system,
                    board.slug,
                    board.extra,
                    result.found_by.value if result.found_by else "",
                    len(result.jobs),
                    at,
                    at,
                    at if verified else None,
                ),
            )
            self._db.executemany(
                """INSERT INTO link_verdicts (domain, url, probability, judged_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(domain, url) DO UPDATE SET probability=excluded.probability,
                       judged_at=excluded.judged_at""",
                [(result.domain, url, p, at) for url, p in result.link_verdicts],
            )

    def get(self, domain: str) -> CachedRoute | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM routes WHERE domain = ?", (domain,)).fetchone()
        if row is None:
            return None
        board = (
            BoardRef(row["board_system"], row["board_slug"], row["board_extra"])
            if row["board_system"]
            else None
        )
        route = ScrapeResult(
            domain=row["domain"],
            status=Status(row["status"]),
            careers_url=row["careers_url"],
            board=board,
            found_by=FoundBy(row["found_by"]) if row["found_by"] else None,
            problem=Problem(row["problem"]) if row["problem"] else None,
            reason=row["reason"],
        )
        return CachedRoute(route, row["scraped_at"], row["last_ok_at"], row["verified_at"], row["job_count"])

    def link_verdicts(self, domain: str) -> dict[str, float]:
        """Every link on this domain's careers page already judged, with its probability."""
        with self._lock:
            rows = self._db.execute("SELECT url, probability FROM link_verdicts WHERE domain = ?", (domain,))
            return {r["url"]: r["probability"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _problem(result: ScrapeResult) -> str:
    return result.problem.value if result.problem else ""
