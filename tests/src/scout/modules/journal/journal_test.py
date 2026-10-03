"""Tests for the journal: tracing across threads, daily files and filters, snapshots,
retention, and the summary of gaps."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scout.modules.journal import Journal, JournalConfig, keep_page, note, summarize, tracing


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def test_trace_collects_notes_from_threads_and_ignores_outside() -> None:
    note("outside", x=1)  # no request: nothing happens
    with tracing() as trace, ThreadPoolExecutor(4) as pool:
        note("start")
        for f in [pool.submit(contextvars.copy_context().run, note, "slug_guess", n=i) for i in range(4)]:
            f.result()
        keep_page("homepage", "<html>1</html>")
        keep_page("homepage", "<html>2</html>")  # the first one is kept
    assert [e["kind"] for e in trace.events].count("slug_guess") == 4
    assert trace.pages == {"homepage": "<html>1</html>"}


def test_records_by_day_filters_and_retention(tmp_path: Path) -> None:
    clock = Clock()
    journal = Journal(JournalConfig(dir=str(tmp_path), retention_days=90, snapshot_retention_days=30), clock)
    journal.record({"request_id": "req_a", "op": "scrape", "domain": "a.dk", "problem": None})
    clock.now += timedelta(days=1)
    journal.record({"request_id": "req_b", "op": "scrape", "domain": "b.dk", "problem": "no_jobs_found"})
    journal.snapshot("req_b", {"careers_page": "<html>b</html>", "../evil": "x"})

    assert [e["domain"] for e in journal.entries()] == ["a.dk", "b.dk"]
    assert [e["domain"] for e in journal.entries(since=date(2026, 10, 4))] == ["b.dk"]
    assert [e["domain"] for e in journal.entries(problem="no_jobs_found")] == ["b.dk"]
    assert journal.snapshot_names("req_b") == ["careers_page"]  # unsafe names are refused
    assert journal.snapshot_page("req_b", "careers_page") == "<html>b</html>"
    assert journal.snapshot_page("../req_b", "careers_page") is None

    clock.now += timedelta(days=40)  # snapshots (30 days) go; entries (90 days) stay
    journal.record({"request_id": "req_c", "op": "scrape", "domain": "c.dk"})
    assert journal.snapshot_names("req_b") == []
    assert len(list(journal.entries())) == 3
    clock.now += timedelta(days=60)
    journal.record({"request_id": "req_d", "op": "scrape", "domain": "d.dk"})
    assert [e["domain"] for e in journal.entries()] == ["c.dk", "d.dk"]


def test_summary_finds_the_gaps() -> None:
    def scrape(domain: str, **kw: object) -> dict:
        return {
            "op": "scrape",
            "domain": domain,
            "at": "2026-10-03T12:00:00",
            "usage": {},
            "jobs": [],
            "trace": [],
            **kw,
        }

    personio = {
        "kind": "foreign_hosts",
        "hosts": ["acme.jobs.personio.de", "www.googletagmanager.com", "acme.teamtailor.com"],
    }
    entries = [
        scrape("a.dk", status="page", trace=[personio]),
        scrape(
            "b.dk", status="page", trace=[{"kind": "foreign_hosts", "hosts": ["b-gmbh.jobs.personio.de"]}]
        ),
        scrape(
            "c.dk",
            status="page",
            request_id="req_c",
            jobs=[{"title": "Job not found"}, {"title": "ISO 9001"}, {"title": "ISO 9001"}],
        ),
        scrape(
            "d.dk",
            status="no_careers_page",
            problem="no_careers_page",
            request_id="req_d",
            snapshots=["homepage"],
        ),
        scrape(
            "e.dk",
            status="board",
            board={"system": "teamtailor"},
            trace=[
                {"kind": "slug_guess", "system": "teamtailor", "outcome": "hit"},
                {"kind": "slug_guess", "system": "greenhouse", "outcome": "missing"},
            ],
        ),
        {"op": "lookup", "domain": "e.dk"},
    ]
    r = summarize(entries)
    assert r["requests"] == {"scrapes": 5, "lookups": 1, "companies": 5}
    assert r["unknown_hosts"][0] == {"host": "jobs.personio.de", "companies": 2, "examples": ["a.dk", "b.dk"]}
    assert all(h["host"] != "teamtailor.com" for h in r["unknown_hosts"])  # known: has an adapter
    assert all("google" not in h["host"] for h in r["unknown_hosts"])  # noise
    flags = r["suspicious_results"][0]["flags"]
    assert r["suspicious_results"][0]["domain"] == "c.dk" and len(flags) == 2
    assert r["misses"] == [
        {
            "domain": "d.dk",
            "problem": "no_careers_page",
            "careers_url": None,
            "request_id": "req_d",
            "snapshots": ["homepage"],
        }
    ]
    assert r["slug_guess"] == {"greenhouse": {"missing": 1}, "teamtailor": {"hit": 1}}
