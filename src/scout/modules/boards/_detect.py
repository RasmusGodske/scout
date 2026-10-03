"""Which hiring system a page belongs to, read from its URL and HTML. Pure."""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlsplit

from scout.modules.boards._adapters import find_emply_section
from scout.modules.results import BoardRef

_S = r"([A-Za-z0-9][A-Za-z0-9_.-]*[A-Za-z0-9]|[A-Za-z0-9])"
_SUB = r"([a-z0-9][a-z0-9-]*[a-z0-9])"
_RESERVED = {
    "www",
    "app",
    "api",
    "apply",
    "jobs",
    "careers",
    "career",
    "assets",
    "cdn",
    "static",
    "help",
    "support",
    "embed",
    "status",
    "blog",
    "docs",
    "images",
    "fonts",
    "screenshots",
    "boards",
    "job-boards",
    "j",
    "v1",
    "v0",
    "en",
    "da",
    "candidate",
    "assets-aws",
    "go",
    "info",
}

# (system, extra, pattern). The first group is the slug. Order does not matter; frequency does.
_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("greenhouse", "", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _S, re.I)),
    (
        "greenhouse",
        "",
        re.compile(
            r"(?:boards|job-boards)\.greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'\s]*&(?:amp;)?)?for="
            + _S,
            re.I,
        ),
    ),
    ("greenhouse", "", re.compile(r"(?://|\b)(?:boards|job-boards)\.greenhouse\.io/(?!embed\b)" + _S, re.I)),
    ("greenhouse", "eu", re.compile(r"job-boards\.eu\.greenhouse\.io/(?!embed\b)" + _S, re.I)),
    ("lever", "", re.compile(r"(?://|\b)jobs\.lever\.co/" + _S, re.I)),
    ("lever", "eu", re.compile(r"(?://|\b)jobs\.eu\.lever\.co/" + _S, re.I)),
    ("lever", "", re.compile(r"api\.lever\.co/v0/postings/" + _S, re.I)),
    ("ashby", "", re.compile(r"jobs\.ashbyhq\.com/" + _S, re.I)),
    ("ashby", "", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _S, re.I)),
    (
        "workable",
        "",
        re.compile(r"apply\.workable\.com/(?:api/v\d/(?:widget/)?accounts/)?(?!api\b|j\b)" + _S, re.I),
    ),
    ("workable", "", re.compile(r"(?://|\b)" + _SUB + r"\.workable\.com", re.I)),
    ("recruitee", "", re.compile(r"(?://|\b)" + _SUB + r"\.recruitee\.com", re.I)),
    ("teamtailor", "", re.compile(r"(?://|\b)" + _SUB + r"\.teamtailor\.com", re.I)),
    ("hrmanager", "", re.compile(r"hr-manager\.net/jobportal\.svc/([A-Za-z0-9_-]+)", re.I)),
    # The candidate site links lists by customer alias: .../list.aspx?customer=<alias>
    (
        "hrmanager",
        "",
        re.compile(r"hr-?manager\.(?:net|com)/[^\"'\s<>]*?[?&](?:amp;)?customer=([A-Za-z0-9_-]+)", re.I),
    ),
    ("emply", "", re.compile(r"(?://|\b)" + _SUB + r"\.career\.emply\.com", re.I)),
    ("join", "", re.compile(r"(?://|\b)join\.com/companies/" + _SUB, re.I)),
    ("hibob", "", re.compile(r"(?://|\b)" + _SUB + r"\.careers\.hibob\.com", re.I)),
]

_TEAMTAILOR_SITE = re.compile(r"teamtailor-cdn\.com", re.I)
_RECRUITEE_SITE = re.compile(r"recruiteecdn\.com", re.I)


def detect_boards(html: str, page_url: str) -> list[BoardRef]:
    """Boards this page is on, embeds or links to, most-referenced first.

    The page's own host counts most: a careers link that landed on `acme.teamtailor.com` is
    that board whatever the page mentions. A Teamtailor or Emply site on the company's own
    host is recognised by its assets or its jobs widget, and keyed by that host.
    """
    counts: Counter[BoardRef] = Counter()
    for ref in _scan(page_url):
        counts[ref] += 1000
    host = urlsplit(page_url).netloc.lower()
    if "teamtailor.com" not in host and len(_TEAMTAILOR_SITE.findall(html)) >= 3:
        counts[BoardRef("teamtailor", host)] += 500
    if "recruitee.com" not in host and len(_RECRUITEE_SITE.findall(html)) >= 3:
        counts[BoardRef("recruitee", host)] += 500
    emply = find_emply_section(html)
    if emply and host:
        counts[BoardRef("emply", host, emply[0])] += 500
    for ref in _scan(html):
        counts[ref] += 1
    return [ref for ref, _ in counts.most_common()]


def _scan(text: str) -> list[BoardRef]:
    out = []
    for system, extra, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            slug = match.group(1).strip(".-_").lower()
            if system in ("workable", "recruitee", "teamtailor", "emply", "join", "hibob"):
                slug = slug.split(".")[0]
            if not slug or slug in _RESERVED or len(slug) > 63:
                continue
            out.append(BoardRef(system, slug, extra))
    return out
