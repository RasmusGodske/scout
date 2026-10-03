from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Where a company's jobs live."""

    BOARD = "board"  # on a known hiring system, read through its public feed
    PAGE = "page"  # on the company's own careers page, one page per job
    NO_CAREERS_PAGE = "no_careers_page"
    FAILED = "failed"


class Problem(StrEnum):
    """Why scout could not get a company's jobs, as something a caller can branch on."""

    UNREACHABLE = "unreachable"  # the site did not answer
    ROBOTS_DISALLOWED = "robots_disallowed"  # robots.txt asks bots like us to stay out
    BLOCKED = "blocked"  # the site refuses bots (403 / bot protection)
    NEEDS_API_TOKEN = "needs_api_token"  # the jobs are behind authentication (401)
    NO_CAREERS_PAGE = "no_careers_page"  # no careers page found on the site
    NO_JOBS_FOUND = "no_jobs_found"  # a careers page, but no job ads recognised on it
    JOB_LIST_UNREADABLE = "job_list_unreadable"  # the page links to a job list we cannot read
    BUDGET_EXCEEDED = "budget_exceeded"  # the request's budget ran out; jobs may be partial
    ERROR = "error"  # anything else; see `reason`


class FoundBy(StrEnum):
    """How discovery first found the route."""

    SLUG_GUESS = "slug_guess"
    HOMEPAGE_LINK = "homepage_link"
    COMMON_PATH = "common_path"
    REDIRECT = "redirect"


class RouteSource(StrEnum):
    """Where this scrape's route came from."""

    CACHED = "cached"  # the route saved by an earlier scrape, still valid
    DISCOVERED = "discovered"  # first scrape of this company (or `fresh` was asked for)
    REDISCOVERED = "rediscovered"  # the saved route was replaced; see `rediscovered_because`


class RediscoveryReason(StrEnum):
    ROUTE_FAILED = "route_failed"  # the saved route broke (404, board deleted, ...)
    ROUTE_EMPTY = "route_empty"  # it suddenly showed no jobs where it had some
    ROUTE_STALE = "route_stale"  # it was old and the homepage no longer leads to it


class DetailSource(StrEnum):
    """Where one job detail came from, most reliable first."""

    BOARD_FEED = "board_feed"  # the hiring system's own field
    JSON_LD = "json_ld"  # the page's schema.org JobPosting markup (what Google for Jobs reads)
    PAGE_HEADING = "page_heading"  # the job page's main heading (title only)
    LINK_TEXT = "link_text"  # the text of the link that led to the job (title only)


class ListingStatus(StrEnum):
    """Whether the company's site still lists a job. Says nothing read from the ad itself."""

    LISTED = "listed"
    NOT_LISTED = "not_listed"  # the board no longer has it, or its page is gone
    CHECK_FAILED = "check_failed"  # scout could not tell; see `problem`


@dataclass(frozen=True)
class JevUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0  # what OpenRouter charged for these calls


@dataclass(frozen=True)
class HttpUsage:
    requests: int = 0  # every outgoing request: pages, feeds, robots.txt, retries
    rendered_pages: int = 0  # pages rendered in the headless browser (the expensive kind)
    bytes: int = 0  # downloaded


@dataclass(frozen=True)
class Usage:
    """What one API request consumed. Measured, never priced: only `jev.usd` is money."""

    jev: JevUsage = field(default_factory=JevUsage)
    http: HttpUsage = field(default_factory=HttpUsage)
    duration_ms: int = 0


@dataclass(frozen=True)
class BoardRef:
    """A company's place on a hiring system: the system's name and the company's slug there.

    `slug` is whatever the system keys a company by: a board token, a subdomain, or a host
    for systems served on the company's own domain. `extra` carries the few systems that need
    a second key (Emply's widget section id).
    """

    system: str
    slug: str
    extra: str = ""


@dataclass(frozen=True)
class JobContent:
    """The job ad as scout got it. `raw` is untouched; `text` is extracted locally, no model."""

    format: str  # "json" or "xml" (a hiring system's feed entry), "html" (a job page)
    raw: str
    text: str


@dataclass(frozen=True)
class Salary:
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: str | None = None  # "year", "month", "hour", ... as the site states it


@dataclass(frozen=True)
class JobDetails:
    """Facts about a job that MAY be filled, each only where the site states it plainly.

    Never read or inferred from the ad's prose. `details_sources` on the Job says where each
    filled field came from; an empty field (None) is simply not stated in a structured way.
    """

    title: str | None = None
    location: str | None = None
    posted_at: str | None = None  # ISO 8601, as the site states it; a date or a UTC timestamp
    employment_type: str | None = None
    department: str | None = None
    salary: Salary | None = None


@dataclass(frozen=True)
class Job:
    """One job. A Record: `id` is unique within its company and stable across scrapes: the
    hiring system's own id on a board, the canonical job URL on a custom page.

    `known` is True when the caller said it already has this job; scout then confirms the job
    is still listed but does not fetch its content again, so `content` is None.
    """

    id: str
    url: str
    content: JobContent | None
    details: JobDetails = field(default_factory=JobDetails)
    details_sources: dict[str, DetailSource] = field(default_factory=dict)
    known: bool = False


@dataclass(frozen=True)
class ScrapeResult:
    """One scrape of one company. The route (status, careers_url, board, found_by) is what
    scout caches; the jobs are returned, not stored."""

    domain: str
    status: Status
    careers_url: str = ""
    board: BoardRef | None = None
    request_id: str = ""
    usage: Usage = field(default_factory=Usage)
    found_by: FoundBy | None = None
    route_source: RouteSource = RouteSource.DISCOVERED
    rediscovered_because: RediscoveryReason | None = None
    jobs: tuple[Job, ...] = field(default_factory=tuple)
    # Set whenever there are no jobs for a reason other than "the company has none open";
    # also on a page with zero jobs, since scout cannot tell that apart from a miss.
    problem: Problem | None = None
    reason: str = ""
    # Jev's (or the fallback's) probability that each judged link on the careers page is a
    # single job ad, cached with the route so a rerun only asks about new links. Not output.
    link_verdicts: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    # True when this scrape discovered the route or confirmed it on the homepage; the route
    # cache uses it to know when to re-check. Not output.
    route_verified: bool = False

    @property
    def found_jobs(self) -> bool:
        return self.status in (Status.BOARD, Status.PAGE) and self.problem is None

    def to_dict(self, *, with_raw: bool = True) -> dict[str, Any]:
        out = asdict(self)
        out.pop("link_verdicts")
        out.pop("route_verified")
        if not with_raw:
            for job in out["jobs"]:
                if job["content"]:
                    job["content"].pop("raw")
        return out


@dataclass(frozen=True)
class JobLookup:
    """One job looked up again by company and job id alone."""

    domain: str
    job_id: str
    listing_status: ListingStatus
    job: Job | None = None  # current content and details when LISTED
    problem: Problem | None = None
    reason: str = ""
    request_id: str = ""
    usage: Usage = field(default_factory=Usage)

    def to_dict(self, *, with_raw: bool = True) -> dict[str, Any]:
        out = asdict(self)
        if not with_raw and out["job"] and out["job"]["content"]:
            out["job"]["content"].pop("raw")
        return out
