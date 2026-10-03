"""Command line: `scout scrape|lookup|route|batch|bench|serve`. Thin; every operation is `Scout`'s."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from scout.app import Scout
from scout.modules.benchmark import Report, judge, read_labels
from scout.modules.metering import Budget
from scout.modules.results import ScrapeResult
from scout.modules.scraper import ScrapeRequest, read_domains


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scout", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("SCOUT_CONFIG", "scout.toml"))
    parser.add_argument("--routes", help="route cache SQLite file (default: routes.path in scout.toml)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scrape", help="scrape one company and print its jobs as JSON")
    p.add_argument("domain")
    p.add_argument("--known", help="file with job ids you already have, one per line")
    p.add_argument("--fresh", action="store_true", help="ignore the cached route")
    p.add_argument("--raw", action="store_true", help="include raw HTML/payloads")
    p.add_argument("--text", action="store_true", help="include each job's extracted text")
    p.add_argument("--max-usd", type=float, help="budget: stop before spending more than this on Jev")
    p.add_argument("--max-requests", type=int, help="budget: stop before more HTTP requests than this")

    p = sub.add_parser("lookup", help="one job's current content by company domain and job id")
    p.add_argument("domain")
    p.add_argument("job_id")
    p.add_argument("--raw", action="store_true")

    p = sub.add_parser("route", help="show the cached route for a company")
    p.add_argument("domain")

    p = sub.add_parser("batch", help="scrape every domain in a CSV, writing JSON Lines")
    p.add_argument("csv")
    p.add_argument("--column", default="domain")
    p.add_argument("--out", help="JSONL file to write results to (default: stdout)")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--workers", type=int)
    p.add_argument("--limit", type=int)

    p = sub.add_parser("bench", help="score scout against the hand-checked benchmark")
    p.add_argument("--labels", default="benchmark/labels.csv")
    p.add_argument("--checked-only", action="store_true", help="only labels Rasmus has checked")
    p.add_argument("--workers", type=int)

    p = sub.add_parser("serve", help="run the HTTP API (SCOUT_API_TOKEN set = bearer token required)")
    p.add_argument("--host", default=os.environ.get("SCOUT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("SCOUT_PORT", "8000")))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING if args.verbose < 2 else logging.DEBUG)

    routes_path = ":memory:" if args.command == "bench" else args.routes
    with Scout.from_config(args.config, routes_path=routes_path) as scout:
        if not scout.jev.available:
            logging.getLogger("scout").warning(
                "Jev unavailable (no API key or spend cap reached): using keyword fallbacks"
            )
        return COMMANDS[args.command](scout, args)


def _scrape(scout: Scout, args: argparse.Namespace) -> int:
    known = Path(args.known).read_text().split() if args.known else []
    budget = (
        Budget(args.max_usd, args.max_requests) if args.max_usd is not None or args.max_requests else None
    )
    result = scout.scrape(args.domain, known, fresh=args.fresh, budget=budget)
    out = result.to_dict(with_raw=args.raw)
    if not args.text:
        for job in out["jobs"]:
            if job["content"]:
                job["content"]["text_chars"] = len(job["content"].pop("text"))
    _print(out)
    return 0 if result.status.value != "failed" else 1


def _lookup(scout: Scout, args: argparse.Namespace) -> int:
    lookup = scout.lookup_job(args.domain, args.job_id)
    _print(lookup.to_dict(with_raw=args.raw))
    return 0 if lookup.listing_status.value != "check_failed" else 1


def _route(scout: Scout, args: argparse.Namespace) -> int:
    cached = scout.route(args.domain)
    if cached is None:
        print(f"{args.domain} has not been scraped", file=sys.stderr)
        return 1
    r = cached.route
    board = f"{r.board.system}/{r.board.slug}" if r.board else "-"
    print(f"{r.domain}: {r.status.value}  board {board}  careers {r.careers_url or '-'}")
    print(f"  found by {r.found_by.value if r.found_by else '-'}; {cached.job_count} jobs last time")
    print(f"  last scrape {cached.scraped_at}, last ok {cached.last_ok_at}, verified {cached.verified_at}")
    if r.problem:
        print(f"  last problem: {r.problem.value} {r.reason}")
    return 0


def _batch(scout: Scout, args: argparse.Namespace) -> int:
    domains = read_domains(args.csv, args.column)[: args.limit]
    sink = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout  # noqa: SIM115
    seen = Counter[str]()
    totals = Counter[str]()

    def emit(result: ScrapeResult) -> None:
        totals["usd"] += result.usage.jev.usd
        totals["http"] += result.usage.http.requests
        totals["rendered"] += result.usage.http.rendered_pages
        sink.write(json.dumps(result.to_dict(), ensure_ascii=False, default=str) + "\n")
        sink.flush()
        seen[result.status.value] += 1
        board = f" {result.board.system}/{result.board.slug}" if result.board else ""
        problem = f" [{result.problem.value}]" if result.problem else ""
        print(
            f"[{sum(seen.values())}/{len(domains)}] {result.domain}: {result.status.value}{board} "
            f"{len(result.jobs)} jobs, route {result.route_source.value}{problem}, "
            f"${result.usage.jev.usd:.5f} {result.usage.http.requests} req {result.usage.duration_ms} ms",
            file=sys.stderr,
            flush=True,
        )

    try:
        scout.scrape_many(
            (ScrapeRequest(d, fresh=args.fresh) for d in domains), workers=args.workers, on_result=emit
        )
    finally:
        if args.out:
            sink.close()
    print(
        f"\n{dict(seen)}. Jev ${totals['usd']:.5f}, {totals['http']} HTTP requests, "
        f"{totals['rendered']} rendered pages.",
        file=sys.stderr,
    )
    return 0


def _bench(scout: Scout, args: argparse.Namespace) -> int:
    labels = read_labels(args.labels)
    if args.checked_only:
        labels = [l for l in labels if l.checked]
    if not labels:
        print("no labels")
        return 1
    results = {
        r.domain: r
        for r in scout.scrape_many(
            (ScrapeRequest(l.domain, fresh=True) for l in labels), workers=args.workers
        )
    }
    for group, chosen in (
        ("checked", [l for l in labels if l.checked]),
        ("draft (unchecked)", [l for l in labels if not l.checked]),
    ):
        if not chosen:
            continue
        report = Report([judge(l, results[l.domain]) for l in chosen if l.domain in results])
        hits = sum(v.hit for v in report.verdicts)
        print(f"\n== {group}: {hits}/{len(report.verdicts)} hit ({report.hit_rate:.0%})")
        for found_by, (h, t) in report.by_found_by().items():
            print(f"   found_by {found_by:14} {h}/{t}")
        for reason, n in report.miss_reasons().most_common():
            print(f"   miss reason {reason:12} {n}")
        for v in report.verdicts:
            if not v.hit:
                print(f"   MISS {v.domain}: {'; '.join(v.misses)}")
    return 0


def _serve(scout: Scout, args: argparse.Namespace) -> int:
    import uvicorn

    from scout.api import create_app

    uvicorn.run(create_app(scout), host=args.host, port=args.port)
    return 0


def _print(obj: object) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, default=str)
    print()


COMMANDS = {
    "scrape": _scrape,
    "lookup": _lookup,
    "route": _route,
    "batch": _batch,
    "bench": _bench,
    "serve": _serve,
}

if __name__ == "__main__":
    sys.exit(main())
