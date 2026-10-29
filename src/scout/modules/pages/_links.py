from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlsplit, urlunsplit

from lxml import html as lxml_html

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "sms:")
_SOCIAL_HOSTS = (
    "facebook.com",
    "linkedin.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "tiktok.com",
    "glassdoor.",
    "pinterest.",
    "vimeo.com",
    "apple.com",
    "google.com",
)
_FILE_SUFFIXES = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".doc", ".docx", ".mp4")
_LANG_PREFIX = re.compile(r"^/(da|en|dk|sv|no|de)(?=/|$)", re.I)


@dataclass(frozen=True)
class Link:
    url: str
    text: str


def collect_links(html: str, base_url: str) -> list[Link]:
    """Every distinct http(s) link on the page with its visible text (or aria-label/title).

    Fragments are dropped, mail/phone/script links and files are skipped, and a URL seen twice
    keeps the longest text it was given.
    """
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return []
    base = doc.xpath("//base/@href")
    base_url = urljoin(base_url, base[0]) if base else base_url
    seen: dict[str, str] = {}
    for a in doc.xpath("//a[@href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith(_SKIP_SCHEMES):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.path.lower().endswith(_FILE_SUFFIXES):
            continue
        text = " ".join((a.text_content() or "").split()) or a.get("aria-label") or a.get("title") or ""
        if len(text) > len(seen.get(url, "")) or url not in seen:
            seen[url] = text[:200]
    return [Link(u, t) for u, t in seen.items()]


def unsupported_job_site_links(links: list[Link], sites: list[str]) -> list[Link]:
    """Links to a job list on a site scout does not read, in page order.

    Each of `sites` is a host and a path pattern, `linkedin.com/company/*/jobs*`: the host
    matches itself and its subdomains (`dk.linkedin.com`), `*` in the path matches anything.
    """
    found: list[Link] = []
    for link in links:
        parts = urlsplit(link.url)
        host = parts.netloc.lower().split(":")[0]
        path = parts.path.lower() or "/"
        for site in sites:
            site_host, _, site_path = site.lower().partition("/")
            if (host == site_host or host.endswith("." + site_host)) and fnmatchcase(path, "/" + site_path):
                found.append(link)
                break
    return found


def careers_candidates(links: list[Link], careers_words: list[str]) -> list[Link]:
    """Links whose URL or text holds a careers word, best first, language variants merged.

    `/da/karriere` and `/en/careers` on the same site count as one candidate (the first kept),
    since either is fine.
    """
    scored: list[tuple[int, int, Link]] = []
    for i, link in enumerate(links):
        if _is_social(link.url):
            continue
        score = careers_score(link, careers_words)
        if score > 0:
            scored.append((-score, i, link))
    scored.sort()
    out: list[Link] = []
    seen_keys: set[str] = set()
    for _, _, link in scored:
        key = _language_neutral(link.url)
        if key not in seen_keys:
            seen_keys.add(key)
            out.append(link)
    return out


def careers_score(link: Link, careers_words: list[str]) -> int:
    """2 per careers word in the link text, 1 per careers word in the URL path or host."""
    text = link.text.lower()
    parts = urlsplit(link.url)
    where = (parts.netloc + parts.path).lower()
    score = 0
    for word in careers_words:
        w = word.lower()
        if _word_in(w, text):
            score += 2
        if _word_in(w.replace(" ", "-"), where):
            score += 1
    return score


def job_link_candidates(links: list[Link], careers_url: str, limit: int) -> list[Link]:
    """Links on a careers page that could be a single job ad: drops social, navigation back
    to the careers page or the homepage, and links to other sites that are not job hosts.
    """
    careers = urlsplit(careers_url)
    careers_key = _language_neutral(careers_url).rstrip("/")
    site = _site(careers.netloc)
    out: list[Link] = []
    for link in links:
        parts = urlsplit(link.url)
        if _is_social(link.url):
            continue
        if _language_neutral(link.url).rstrip("/") == careers_key:
            continue
        if parts.path in ("", "/") and not parts.query:
            continue
        if _site(parts.netloc) != site and not _looks_like_job_url(link.url):
            continue
        out.append(link)
    return out[:limit]


def job_link_heuristic(link: Link, careers_url: str) -> float:
    """A rough probability that a link is a single job ad, used only when Jev is unavailable.

    A link that sits below the careers page or holds a job-ish path segment followed by a
    further segment (an id or a slug) scores high.
    """
    url = link.url.lower()
    careers_path = urlsplit(careers_url).path.rstrip("/").lower()
    path = urlsplit(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if _looks_like_job_url(url):
        return 0.8
    if (
        careers_path
        and path.startswith(careers_path + "/")
        and len(segments) > len([s for s in careers_path.split("/") if s])
    ):
        return 0.7
    if any(re.search(r"\d{3,}", s) for s in segments) and any(
        k in url for k in ("job", "stilling", "position", "vacanc", "career", "karriere")
    ):
        return 0.7
    return 0.1


_TRACKING = re.compile(r"^(utm_.*|gclid|fbclid|msclkid|mc_[ce]id|_hs.*|ref|referrer|source|src|trk)$", re.I)


def canonical_url(url: str) -> str:
    """A job page's stable id: scheme and host lower-cased, `www.` kept as given, fragment,
    tracking parameters and a trailing slash dropped, remaining query parameters sorted.
    """
    parts = urlsplit(urldefrag(url)[0].strip())
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def sitemap_entries(xml: str) -> tuple[list[str], list[str]]:
    """(page URLs, child sitemap URLs) from a sitemap or sitemap index; empty for anything else."""
    from lxml import etree

    try:
        root = etree.fromstring(xml.encode("utf-8"), parser=etree.XMLParser(recover=True))
    except (ValueError, etree.XMLSyntaxError):
        return [], []
    if root is None:
        return [], []
    locs = [str(x).strip() for x in root.xpath("//*[local-name()='loc']/text()")]
    if etree.QName(root).localname == "sitemapindex":
        return [], locs
    return locs, []


def foreign_hosts(html: str, page_url: str) -> list[str]:
    """Hosts on other sites that the page links to (a), embeds (iframe) or loads scripts from,
    most frequent first. Where a careers page hands off to a hiring system shows up here."""
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return []
    site = _site(urlsplit(page_url).netloc)
    counts: dict[str, int] = {}
    for value in doc.xpath("//a/@href | //iframe/@src | //script/@src"):
        url = urljoin(page_url, str(value).strip())
        parts = urlsplit(url)
        host = parts.netloc.lower().split(":")[0]
        if parts.scheme in ("http", "https") and host and _site(host) != site:
            counts[host] = counts.get(host, 0) + 1
    return [h for h, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def same_site(a: str, b: str) -> bool:
    return _site(urlsplit(a).netloc) == _site(urlsplit(b).netloc)


# A path segment naming a job (`/job/<slug>`, `/apply/<slug>/<id>` on Talentadore, `/tjanst/<id>`
# on a Swedish recruiter's site), followed by the job's own segment. Not `tjanster`: the plural
# is Swedish for services (`/tjanster/it-support`), as French `offres` are mostly offers.
_JOB_URL = re.compile(
    r"/(job|jobs|jobb|stilling|stillinger|position|positions|vacancy|vacancies|vacature|vacatures|"
    r"opening|openings|posting|postings|requisition|requisitions|apply|tjanst|stelle|stellen|"
    r"stellenangebot|emploi|careers?|karriere|ad|o|j)/[^/?#]{3,}",
    re.I,
)
# A script page for one job, picked by an id in the query: `job.aspx?id=29907` (Reqruiting),
# `vacancy.php?id=12`, `ShowJob.aspx?jobId=7`.
_JOB_SCRIPT = re.compile(
    r"/[\w-]*(job|vacanc|position|stilling|posting)[\w-]*\.(aspx?|php|html?|jsp|cfm)$", re.I
)
_ID_QUERY = re.compile(r"(^|&)[\w-]*id=[\w-]+", re.I)


def _looks_like_job_url(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path
    if _JOB_SCRIPT.search(path) and _ID_QUERY.search(parts.query):
        return True
    return bool(_JOB_URL.search(path)) and not path.rstrip("/").lower().endswith(
        ("/jobs", "/careers", "/karriere", "/stillinger")
    )


def _word_in(word: str, haystack: str) -> bool:
    return re.search(r"(?<![a-zæøå])" + re.escape(word) + r"(?![a-zæøå])", haystack) is not None


def _language_neutral(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.lower().removeprefix("www.") + _LANG_PREFIX.sub("", parts.path)


def _site(netloc: str) -> str:
    host = netloc.lower().split(":")[0]
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def _is_social(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == s or host.endswith("." + s) or s in host for s in _SOCIAL_HOSTS)
