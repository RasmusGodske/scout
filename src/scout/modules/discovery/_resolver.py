from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from scout.modules.boards import BoardError, Boards, Listing, detect_boards
from scout.modules.fetch import Blocked, Fetcher, FetchError, Response, Unreachable
from scout.modules.jev import Jev, JevError, JevUnavailable
from scout.modules.metering import BudgetExceeded
from scout.modules.pages import (
    Link,
    canonical_url,
    careers_candidates,
    careers_score,
    collect_links,
    details_from_page,
    extract_text,
    job_link_candidates,
    job_link_heuristic,
    page_title,
    same_site,
)
from scout.modules.results import (
    BoardRef,
    DetailSource,
    FoundBy,
    Job,
    JobContent,
    JobDetails,
    JobLookup,
    ListingStatus,
    Problem,
    RediscoveryReason,
    RouteSource,
    ScrapeResult,
    Status,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveryConfig:
    careers_words: list[str]
    common_paths: list[str]
    job_link_threshold: float = 0.6
    careers_choice_min_probability: float = 0.35
    max_job_links_per_page: int = 150

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> DiscoveryConfig:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


@dataclass(frozen=True)
class Known:
    """What the caller already knows about a company.

    `previous` is the cached route (a ScrapeResult without jobs) and `previous_job_count` how
    many jobs it gave last time; `route_stale` says the route is old enough to re-check
    against the homepage. `job_ids` are jobs the caller already has: they are confirmed as
    still listed but not fetched again. `link_verdicts` are Jev's cached link judgments.
    """

    previous: ScrapeResult | None = None
    previous_job_count: int = 0
    route_stale: bool = False
    job_ids: frozenset[str] = frozenset()
    link_verdicts: dict[str, float] = field(default_factory=dict)


class Resolver:
    """Domain in, `ScrapeResult` out. Reads the web and asks Jev; writes nothing.

    With a cached route it goes straight there, and falls back to discovery when the route
    broke (`route_failed`), suddenly shows no jobs (`route_empty`), or is stale and the
    homepage no longer leads to it (`route_stale`). The result's `route_source` says which.
    """

    def __init__(self, fetcher: Fetcher, boards: Boards, jev: Jev, config: DiscoveryConfig) -> None:
        self._fetcher = fetcher
        self._boards = boards
        self._jev = jev
        self._config = config

    def resolve(self, domain: str, known: Known | None = None, *, fresh: bool = False) -> ScrapeResult:
        domain = normalize_domain(domain)
        known = known or Known()
        try:
            prev = known.previous
            if prev is not None and not fresh and prev.status in (Status.BOARD, Status.PAGE):
                return self._from_cached_route(domain, prev, known)
            return replace(self._discover(domain, known), route_verified=True)
        except BudgetExceeded as exc:
            return _failed(domain, Problem.BUDGET_EXCEEDED, str(exc))
        except Blocked as exc:
            return _failed(domain, Problem.ROBOTS_DISALLOWED, str(exc))
        except _NoHomepage as exc:
            return _failed(domain, exc.problem, str(exc))
        except Unreachable as exc:
            return _failed(domain, Problem.UNREACHABLE, str(exc))
        except (FetchError, BoardError) as exc:
            return _failed(domain, Problem.ERROR, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop a batch
            log.exception("resolving %s", domain)
            return _failed(domain, Problem.ERROR, f"{exc.__class__.__name__}: {exc}")

    def lookup_job(self, route: ScrapeResult, job_id: str) -> JobLookup:
        """One job again, by its company's cached route and its id alone.

        On a board: the vendor's single-job endpoint where one exists, else its list. On a
        custom page the id is the job's URL: a 404/410, or a redirect to another page (usually
        the careers page or the homepage), means it is no longer listed.
        """
        domain = route.domain
        if route.status is Status.PAGE:
            if urlsplit(job_id.strip()).scheme not in ("http", "https"):
                return JobLookup(
                    domain,
                    job_id,
                    ListingStatus.CHECK_FAILED,
                    problem=Problem.ERROR,
                    reason="this company's jobs are on its own careers page, so a job id is the job's URL",
                )
            job_id = canonical_url(job_id)  # accept the URL as a caller may have copied it
        try:
            if route.status is Status.BOARD and route.board is not None:
                job = self._boards.fetch_job(route.board, job_id)
            elif route.status is Status.PAGE:
                job = self._refetch_page_job(job_id)
            else:
                return JobLookup(
                    domain,
                    job_id,
                    ListingStatus.CHECK_FAILED,
                    problem=route.problem or Problem.NO_CAREERS_PAGE,
                    reason=f"no route to this company's jobs ({route.status.value})",
                )
        except BudgetExceeded as exc:
            return JobLookup(
                domain, job_id, ListingStatus.CHECK_FAILED, None, Problem.BUDGET_EXCEEDED, str(exc)[:500]
            )
        except Blocked as exc:
            return JobLookup(
                domain, job_id, ListingStatus.CHECK_FAILED, None, Problem.ROBOTS_DISALLOWED, str(exc)[:500]
            )
        except Unreachable as exc:
            return JobLookup(
                domain, job_id, ListingStatus.CHECK_FAILED, None, Problem.UNREACHABLE, str(exc)[:500]
            )
        except (FetchError, BoardError) as exc:
            return JobLookup(domain, job_id, ListingStatus.CHECK_FAILED, None, Problem.ERROR, str(exc)[:500])
        if job is None:
            return JobLookup(domain, job_id, ListingStatus.NOT_LISTED)
        return JobLookup(domain, job_id, ListingStatus.LISTED, job)

    def _refetch_page_job(self, job_id: str) -> Job | None:
        response = self._fetcher.page(job_id)
        if response.status in (404, 410):
            return None
        if not response.ok:
            raise FetchError(f"{job_id} answered {response.status}")
        if canonical_url(response.final_url) != job_id and _key(response.final_url) != _key(job_id):
            return None
        return _page_job(job_id, response, "")

    # --- the cached route -----------------------------------------------------------------------

    def _from_cached_route(self, domain: str, prev: ScrapeResult, known: Known) -> ScrapeResult:
        verified = False
        if known.route_stale:
            # Re-check an old route against the homepage: a company that moved to a new hiring
            # system usually changes its homepage link, while the old board may live on.
            homepage = self._homepage(domain)
            if not isinstance(homepage, _NoHomepage):
                if not _homepage_leads_to(homepage, prev):
                    return self._rediscover(domain, known, prev, RediscoveryReason.ROUTE_STALE, homepage)
                verified = True
        result = self._follow_route(domain, prev, known)
        if result is None:
            return self._rediscover(domain, known, prev, RediscoveryReason.ROUTE_FAILED)
        if not result.jobs and known.previous_job_count > 0:
            return self._rediscover(domain, known, prev, RediscoveryReason.ROUTE_EMPTY)
        return replace(result, route_source=RouteSource.CACHED, route_verified=verified)

    def _follow_route(self, domain: str, prev: ScrapeResult, known: Known) -> ScrapeResult | None:
        """The jobs along the cached route; None when the route itself is gone."""
        if prev.status is Status.BOARD and prev.board is not None:
            listing = self._boards.fetch(prev.board)
            if listing is None:
                return None
            return self._board_result(domain, listing, prev.found_by, known, prev.careers_url)
        response = self._fetcher.page(prev.careers_url)
        if response.status in (404, 410):
            return None
        if not response.ok:
            raise FetchError(f"{prev.careers_url} answered {response.status}")
        if _key(response.final_url) in (_key(f"https://{domain}/"), _key(f"https://www.{domain}/")):
            return None  # the careers page now redirects to the homepage
        return self._from_careers_page(domain, response, prev.found_by, known)

    def _rediscover(
        self,
        domain: str,
        known: Known,
        prev: ScrapeResult,
        because: RediscoveryReason,
        homepage: Response | None = None,
    ) -> ScrapeResult:
        log.info("rediscovering %s: %s", domain, because.value)
        # A stale board the homepage no longer leads to must not win back by slug guess: it
        # may be the abandoned one. It still counts if the site's own links lead to it.
        distrust = prev.board if because is RediscoveryReason.ROUTE_STALE else None
        result = self._discover(domain, known, homepage, distrust=distrust)
        if result.status is Status.FAILED:
            return replace(result, rediscovered_because=because)
        if _same_route(result, prev):
            # Discovery led back to the same place: the route stands, now freshly verified.
            return replace(result, route_source=RouteSource.CACHED, route_verified=True)
        return replace(
            result, route_source=RouteSource.REDISCOVERED, rediscovered_because=because, route_verified=True
        )

    # --- discovery, cheapest first --------------------------------------------------------------

    def _discover(
        self,
        domain: str,
        known: Known,
        homepage: Response | None = None,
        *,
        distrust: BoardRef | None = None,
    ) -> ScrapeResult:
        # 1. Guess the board from the domain, then weigh it against what the homepage links to.
        # The homepage is the stronger evidence: a slug-guessed board can be a stale one the
        # company left (Molslinjen's old Teamtailor next to its live Emply), and a feed that
        # names no company (Lever, Ashby) may be someone else's (`lunar` on Ashby is not
        # lunar.app). So a guess wins outright only when the homepage links to it, or when the
        # homepage links to no board and the feed names this company.
        listing = self._boards.guess(domain)
        if listing is not None and distrust is not None and _same_board(listing.ref, distrust):
            listing = None
        homepage = homepage or self._homepage(domain)
        if isinstance(homepage, _NoHomepage):
            if listing is not None and listing.identifies_company:
                return self._board_result(domain, listing, FoundBy.SLUG_GUESS, known)
            raise homepage
        linked = detect_boards(homepage.text, homepage.final_url)
        if listing is not None and listing.ref in linked:
            return self._board_result(domain, listing, FoundBy.SLUG_GUESS, known)
        linked_listing = self._first_readable(linked)
        if linked_listing is not None:
            return self._board_result(domain, linked_listing, FoundBy.HOMEPAGE_LINK, known)
        if listing is not None and listing.identifies_company:
            return self._board_result(domain, listing, FoundBy.SLUG_GUESS, known)

        # 2. The careers link on the homepage.
        careers = self._careers_link(domain, homepage)
        found_by = FoundBy.HOMEPAGE_LINK
        response = None
        if careers is not None:
            response = self._try_page(careers)

        # 3. Common paths.
        if response is None:
            found_by = FoundBy.COMMON_PATH
            response = self._common_path(homepage)
        if response is None:
            return ScrapeResult(
                domain,
                Status.NO_CAREERS_PAGE,
                problem=Problem.NO_CAREERS_PAGE,
                reason="no careers link on the homepage and none of the common paths exist",
            )

        # 4 and 5. A board behind the careers page, or the page's own job links.
        return self._from_careers_page(domain, response, found_by, known)

    def _from_careers_page(
        self, domain: str, response: Response, found_by: FoundBy | None, known: Known, hop: int = 0
    ) -> ScrapeResult:
        careers_url = response.final_url
        redirected = not same_site(response.url, response.final_url)
        listing = self._board_behind(response)
        if listing is not None:
            how = FoundBy.REDIRECT if redirected and _is_board_url(response.final_url) else found_by
            return self._board_result(domain, listing, how, known, careers_url)

        links = collect_links(response.text, careers_url)
        candidates = job_link_candidates(links, careers_url, self._config.max_job_links_per_page)
        verdicts = self._judge_job_links(candidates, response, known)
        job_links = [l for l in candidates if verdicts.get(l.url, 0.0) >= self._config.job_link_threshold]

        if not job_links and not response.rendered:
            # The page had enough navigation not to look empty, but its job list may be
            # injected by a script (an ATS widget). Render it once and look again.
            rendered = self._try_page(response.final_url, render=True)
            if rendered is not None and rendered.rendered:
                return self._from_careers_page(domain, rendered, found_by, known, hop)

        reason, problem = "", None
        if not job_links and hop == 0:
            # A careers landing page often links to the real list ("See open positions").
            listing_page = self._listing_link(domain, response, links)
            if listing_page is not None:
                nested = self._try_page(listing_page)
                if nested is None:
                    reason = f"job list at {listing_page} could not be read (blocked or gone)"
                    problem = Problem.JOB_LIST_UNREADABLE
                else:
                    result = self._from_careers_page(domain, nested, found_by, known, hop=1)
                    if result.jobs or result.status is Status.BOARD:
                        return result

        jobs_found: list[Job] = []
        for link in _dedupe(job_links):
            try:
                job = self._job(link, known)
            except BudgetExceeded as exc:
                # Keep what was paid for: the route and the jobs fetched so far.
                problem, reason = (
                    Problem.BUDGET_EXCEEDED,
                    f"{exc}; {len(jobs_found)} of {len(job_links)} jobs fetched",
                )
                break
            if job is not None:
                jobs_found.append(job)
        jobs = tuple(jobs_found)
        if not jobs and problem is None:
            problem = Problem.NO_JOBS_FOUND
            reason = reason or f"no job ads recognised among {len(candidates)} links on the careers page"
        new_verdicts = tuple((url, p) for url, p in verdicts.items() if url not in known.link_verdicts)
        return ScrapeResult(
            domain,
            Status.PAGE,
            careers_url=careers_url,
            found_by=found_by,
            reason=reason,
            problem=problem,
            jobs=jobs,
            link_verdicts=new_verdicts,
        )

    def _board_behind(self, response: Response) -> Listing | None:
        return self._first_readable(detect_boards(response.text, response.final_url))

    def _first_readable(self, refs: list[BoardRef]) -> Listing | None:
        for ref in refs[:3]:
            try:
                listing = self._boards.fetch(ref)
            except BoardError as exc:
                log.info("detected %s/%s but could not read it: %s", ref.system, ref.slug, exc)
                continue
            if listing is not None:
                return listing
        return None

    def _board_result(
        self, domain: str, listing: Listing, found_by: FoundBy | None, known: Known, careers_url: str = ""
    ) -> ScrapeResult:
        # The feed gives every job's content in one response either way; jobs the caller
        # already has come back as references so the answer stays small.
        jobs = tuple(
            replace(j, content=None, known=True) if j.id in known.job_ids else j for j in listing.jobs
        )
        return ScrapeResult(
            domain,
            Status.BOARD,
            careers_url=careers_url or self._boards.public_url(listing.ref),
            board=listing.ref,
            found_by=found_by,
            jobs=jobs,
        )

    # --- pages ----------------------------------------------------------------------------------

    def _homepage(self, domain: str) -> Response | _NoHomepage:
        """The homepage, trying https, www and http; or why none of them could be read."""
        statuses: list[int] = []
        errors: list[str] = []
        for url in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
            try:
                response = self._fetcher.page(url)
            except Blocked:
                raise
            except FetchError as exc:
                log.info("homepage %s: %s", url, exc)
                errors.append(str(exc))
                continue
            if response.ok:
                return response
            statuses.append(response.status)
        if 401 in statuses:
            return _NoHomepage(Problem.NEEDS_API_TOKEN, f"homepage answered {statuses}")
        if any(s in (403, 429, 503) for s in statuses):
            return _NoHomepage(Problem.BLOCKED, f"homepage answered {statuses} (bot protection?)")
        if statuses:
            return _NoHomepage(Problem.ERROR, f"homepage answered {statuses}")
        return _NoHomepage(Problem.UNREACHABLE, "; ".join(errors)[:500] or "homepage unreachable")

    def _try_page(self, url: str, *, render: bool = False) -> Response | None:
        try:
            response = self._fetcher.page(url, render=render)
        except Blocked:
            log.info("robots.txt disallows %s", url)
            return None
        except FetchError as exc:
            log.info("careers page %s: %s", url, exc)
            return None
        return response if response.ok else None

    def _common_path(self, homepage: Response) -> Response | None:
        parts = urlsplit(homepage.final_url)
        base = f"{parts.scheme}://{parts.netloc}"
        home_key = _key(homepage.final_url)
        for path in self._config.common_paths:
            response = self._try_page(base + path)
            if response is not None and _key(response.final_url) != home_key:
                return response
        return None

    def _job(self, link: Link, known: Known) -> Job | None:
        job_id = canonical_url(link.url)
        if job_id in known.job_ids:
            title = " ".join(link.text.split()) or None
            return Job(
                job_id,
                link.url,
                content=None,
                details=JobDetails(title=title),
                details_sources={"title": DetailSource.LINK_TEXT} if title else {},
                known=True,
            )
        response = self._try_page(link.url)
        if response is None:
            return None
        return _page_job(job_id, response, link.text)

    # --- judgments ------------------------------------------------------------------------------

    def _careers_link(self, domain: str, homepage: Response) -> str | None:
        links = collect_links(homepage.text, homepage.final_url)
        candidates = careers_candidates(links, self._config.careers_words)
        if len(candidates) == 1:
            return candidates[0].url
        pool = candidates or [
            l for l in links if same_site(l.url, homepage.final_url) or _is_board_url(l.url)
        ]
        if not pool:
            return None
        chosen = self._choose_link(
            domain,
            homepage,
            pool,
            "Which of these links on the company's website leads to the page where the company "
            "lists its open jobs (its careers, jobs or vacancies page)? A page on a separate "
            "hiring site (such as Teamtailor, Greenhouse or Emply) counts.",
            purpose="careers_link",
        )
        if chosen is None and candidates and not self._jev.available:
            return candidates[0].url
        return chosen

    def _listing_link(self, domain: str, page: Response, links: list[Link]) -> str | None:
        here = _key(page.final_url)
        # Off-site links count too: a company acquired by another may list its jobs on the
        # parent's careers site (Planday's are on careers.xero.com).
        pool = [l for l in careers_candidates(links, self._config.careers_words) if _key(l.url) != here]
        if not pool:
            return None
        if len(pool) == 1 or not self._jev.available:
            return pool[0].url
        return self._choose_link(
            domain,
            page,
            pool,
            "This is the company's careers page, but it shows no job ads itself. Which of these "
            "links leads to the list of the company's open positions?",
            purpose="listing_link",
        )

    def _choose_link(
        self, domain: str, page: Response, pool: list[Link], question: str, purpose: str
    ) -> str | None:
        pool = pool[: self._jev.max_choice_options - 1]
        options: dict[str, Any] = {
            f"link_{i}": {"url": l.url, "text": l.text or "(no text)"} for i, l in enumerate(pool)
        }
        options["none"] = "None of these links leads there."
        try:
            answer = self._jev.choose(
                {"company_website": domain, "page_url": page.final_url, "page_title": page_title(page.text)},
                question,
                options,
                purpose=purpose,
            )
        except (JevUnavailable, JevError) as exc:
            log.info("jev unavailable for %s (%s); using the best keyword match", purpose, exc)
            return pool[0].url if pool and _has_careers_word(pool[0], self._config.careers_words) else None
        if answer.choice == "none":
            return None
        if answer.probabilities.get(answer.choice, 0.0) < self._config.careers_choice_min_probability:
            return None
        return pool[int(answer.choice.removeprefix("link_"))].url

    def _judge_job_links(self, links: list[Link], page: Response, known: Known) -> dict[str, float]:
        verdicts = {l.url: known.link_verdicts[l.url] for l in links if l.url in known.link_verdicts}
        new = [l for l in links if l.url not in verdicts]
        if not new:
            return verdicts
        questions = {
            f"l{i}": {
                "link": {"url": l.url, "text": l.text or "(no text)"},
                "question": (
                    "Is `link` a link to a single job ad: a page about one specific open "
                    "position at this company? Links to the list of all jobs, to job "
                    "categories, locations or departments, to general information about "
                    "working at the company, to applying without a specific position, or to "
                    "any other page are not."
                ),
            }
            for i, l in enumerate(new)
        }
        try:
            answers = self._jev.nouls(
                {"careers_page_url": page.final_url, "careers_page_title": page_title(page.text)},
                questions,
                purpose="job_link",
            )
            verdicts.update({new[int(qid[1:])].url: p for qid, p in answers.items()})
        except (JevUnavailable, JevError) as exc:
            log.info("jev unavailable for job links (%s); using the URL heuristic", exc)
            verdicts.update({l.url: job_link_heuristic(l, page.final_url) for l in new})
        return verdicts


class _NoHomepage(Exception):
    def __init__(self, problem: Problem, reason: str) -> None:
        super().__init__(reason)
        self.problem = problem


def _failed(domain: str, problem: Problem, reason: str) -> ScrapeResult:
    return ScrapeResult(domain, Status.FAILED, reason=reason[:500], problem=problem)


def _page_job(job_id: str, response: Response, link_text: str) -> Job:
    details, sources = details_from_page(response.text, response.final_url, link_text)
    return Job(
        id=job_id,
        url=response.final_url,
        content=JobContent("html", response.text, extract_text(response.text)),
        details=details,
        details_sources=sources,
    )


def _same_board(a: BoardRef | None, b: BoardRef | None) -> bool:
    return a is not None and b is not None and (a.system, a.slug) == (b.system, b.slug)


def _same_route(a: ScrapeResult, b: ScrapeResult) -> bool:
    if a.status is not b.status:
        return False
    if a.status is Status.BOARD:
        return _same_board(a.board, b.board)
    return _key(a.careers_url) == _key(b.careers_url)


def _homepage_leads_to(homepage: Response, route: ScrapeResult) -> bool:
    """Whether the homepage still links to the cached route: to its board, its board's host, or
    its careers page (or a page above it on the same site)."""
    if route.board is not None and route.board in detect_boards(homepage.text, homepage.final_url):
        return True
    targets = {_key(route.careers_url)} if route.careers_url else set()
    if route.board is not None and "." in route.board.slug:
        targets.add(route.board.slug)  # a board served on the company's own host
    for link in collect_links(homepage.text, homepage.final_url):
        key = _key(link.url)
        host, _, path = key.partition("/")
        for target in targets:
            if key == target or host == target:
                return True
            # The homepage links a page above the careers page: /karriere for /karriere/jobs.
            if path and target.startswith(key.rstrip("/") + "/"):
                return True
    return False


def normalize_domain(domain: str) -> str:
    d = domain.strip().lower()
    if "://" in d:
        d = urlsplit(d).netloc
    return d.split("/")[0].split(":")[0].removeprefix("www.")


def _is_board_url(url: str) -> bool:
    return bool(detect_boards("", url))


def _key(url: str) -> str:
    parts = urlsplit(url)
    return parts.netloc.lower().removeprefix("www.") + (parts.path.rstrip("/") or "/")


def _dedupe(links: list[Link]) -> list[Link]:
    seen: set[str] = set()
    out = []
    for link in links:
        key = canonical_url(link.url)
        if key not in seen:
            seen.add(key)
            out.append(link)
    return out


def _has_careers_word(link: Link, words: list[str]) -> bool:
    return careers_score(link, words) > 0
