"""Journal entries in, a summary of what to fix next out. Pure."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

# Hosts that show up on every website and say nothing about hiring.
_NOISE = re.compile(
    r"(google|gstatic|googleapis|googletagmanager|doubleclick|facebook|fbcdn|instagram|linkedin|"
    r"twitter|x\.com|youtube|ytimg|vimeo|tiktok|pinterest|cookiebot|cookielaw|onetrust|usercentrics|"
    r"cloudflare|jsdelivr|unpkg|cdnjs|bootstrapcdn|fontawesome|typekit|hotjar|hubspot|hs-scripts|"
    r"hs-analytics|hsforms|hs-banner|intercom|zendesk|wp\.com|wordpress|gravatar|jquery|vimeocdn|"
    r"maps\.|apple\.com|microsoft|bing|clarity\.ms|matomo|plausible|segment|sentry|newrelic|"
    r"trustpilot|calendly|stripe|paypal|shopify|squarespace|wixstatic|webflow|framerusercontent|"
    r"cdn\.|static\.|fonts\.|assets\.|capterra|g2\.com|trustradius|kununu|glassdoor|indeed|"
    r"cookiedatabase|termly|iubenda|complianz|hscollectedforms|hsadspixel|hs-sites|hubspot|"
    r"clearbit|cloudfront|amazonaws|amazon\.|medium\.com|substack|bsky|threads\.net|wa\.me|"
    r"whatsapp|t\.me|telegram|mailchimp|list-manage|sendinblue|brevo|typeform|^chrome-extension)",
    re.I,
)
_KNOWN_BOARDS = re.compile(
    r"(greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|recruitee\.com|teamtailor\.com|"
    r"teamtailor-cdn\.com|hr-manager\.net|emply\.com|join\.com|hibob\.com)",
    re.I,
)
_NOT_A_JOB = re.compile(
    r"(not found|404|no longer available|page not found|ikke fundet|nicht gefunden|introuvable|"
    r"expired|closed|udløbet|abgelaufen)",
    re.I,
)


def summarize(entries: Iterable[dict[str, Any]], *, top: int = 25) -> dict[str, Any]:
    """What happened across many requests, and the gaps worth fixing, most common first."""
    scrapes: list[dict[str, Any]] = []
    lookups: list[dict[str, Any]] = []
    for e in entries:
        (scrapes if e.get("op") == "scrape" else lookups).append(e)
    latest = _latest_per_domain(scrapes)

    slug = defaultdict(Counter)
    careers_choice = Counter()
    hosts: dict[str, set[str]] = defaultdict(set)
    for e in scrapes:
        for ev in e.get("trace", []):
            kind = ev.get("kind")
            if kind == "slug_guess":
                slug[ev.get("system", "?")][ev.get("outcome", "?")] += 1
            elif kind == "careers_link":
                careers_choice[ev.get("method", "?")] += 1
            elif kind == "foreign_hosts":
                for host in ev.get("hosts", []):
                    if not _NOISE.search(host) and not _KNOWN_BOARDS.search(host):
                        hosts[_site(host)].add(e.get("domain", ""))

    suspicious = []
    for e in latest.values():
        titles = [j.get("title") or "" for j in e.get("jobs", [])]
        flags = []
        if any(_NOT_A_JOB.search(t) for t in titles):
            flags.append("title says the job is gone")
        dupes = [t for t, n in Counter(titles).items() if t and n > 1]
        if dupes:
            flags.append(f"repeated titles: {dupes[:3]}")
        if flags:
            suspicious.append(
                {
                    "domain": e["domain"],
                    "request_id": e.get("request_id"),
                    "careers_url": e.get("careers_url"),
                    "flags": flags,
                }
            )

    usage = [e.get("usage", {}) for e in scrapes]
    return {
        "requests": {"scrapes": len(scrapes), "lookups": len(lookups), "companies": len(latest)},
        "status": dict(Counter(e.get("status") for e in latest.values()).most_common()),
        "problems": dict(
            Counter(e.get("problem") for e in latest.values() if e.get("problem")).most_common()
        ),
        "companies_with_jobs": sum(1 for e in latest.values() if e.get("jobs")),
        "boards": dict(
            Counter(
                (e.get("board") or {}).get("system") for e in latest.values() if e.get("board")
            ).most_common()
        ),
        "found_by": dict(
            Counter(e.get("found_by") for e in latest.values() if e.get("found_by")).most_common()
        ),
        "route_source": dict(Counter(e.get("route_source") for e in scrapes).most_common()),
        "rediscovered_because": dict(
            Counter(
                e.get("rediscovered_because") for e in scrapes if e.get("rediscovered_because")
            ).most_common()
        ),
        "usage": {
            "jev_usd": round(sum(u.get("jev", {}).get("usd", 0) for u in usage), 6),
            "http_requests": sum(u.get("http", {}).get("requests", 0) for u in usage),
            "rendered_pages": sum(u.get("http", {}).get("rendered_pages", 0) for u in usage),
            "seconds": round(sum(u.get("duration_ms", 0) for u in usage) / 1000, 1),
        },
        # Which systems the slug guess ever finds; one that never hits only costs time.
        "slug_guess": {s: dict(c) for s, c in sorted(slug.items())},
        "careers_link_chosen_by": dict(careers_choice.most_common()),
        # Off-site hosts on careers pages that scout has no adapter for, by how many companies
        # use them: the candidates for the next adapter.
        "unknown_hosts": [
            {"host": h, "companies": len(d), "examples": sorted(d)[:5]}
            for h, d in sorted(hosts.items(), key=lambda kv: -len(kv[1]))[:top]
        ],
        "suspicious_results": suspicious[:top],
        # The latest miss per company, with the request id whose snapshots show the pages.
        "misses": [
            {
                "domain": e["domain"],
                "problem": e.get("problem"),
                "careers_url": e.get("careers_url"),
                "request_id": e.get("request_id"),
                "snapshots": e.get("snapshots", []),
            }
            for e in latest.values()
            if e.get("problem") in ("no_careers_page", "no_jobs_found", "job_list_unreadable")
        ][: top * 4],
        "slowest": [
            {
                "domain": e["domain"],
                "seconds": round(e.get("usage", {}).get("duration_ms", 0) / 1000, 1),
                "requests": e.get("usage", {}).get("http", {}).get("requests", 0),
            }
            for e in sorted(scrapes, key=lambda e: -e.get("usage", {}).get("duration_ms", 0))[:10]
        ],
        "errors": dict(
            Counter(
                e.get("reason", "")[:120] for e in latest.values() if e.get("problem") == "error"
            ).most_common(top)
        ),
    }


def _latest_per_domain(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for e in entries:
        domain = e.get("domain")
        if domain and (domain not in latest or e.get("at", "") >= latest[domain].get("at", "")):
            latest[domain] = e
    return latest


def _site(host: str) -> str:
    """Group hosts the way an adapter would, by dropping the first label of a 3+ label host:
    `acme.jobs.personio.de` -> `jobs.personio.de`, `app.hrlab.de` -> `hrlab.de`. A host under a
    two-part suffix (`acme.co.uk`) is kept whole."""
    labels = host.lower().removeprefix("www.").split(".")
    if len(labels) < 3 or (len(labels) == 3 and labels[-2] in ("co", "com", "org", "ac", "net")):
        return ".".join(labels)
    return ".".join(labels[1:])
