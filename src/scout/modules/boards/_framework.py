from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scout.modules.fetch import Fetcher, Response
from scout.modules.results import BoardRef, Job


@dataclass(frozen=True)
class Listing:
    """What one hiring system says about one company: its jobs and, if given, its name and
    website (used to check a slug-guessed board belongs to the domain)."""

    ref: BoardRef
    jobs: tuple[Job, ...]
    company_name: str = ""
    company_site: str = ""

    @property
    def identifies_company(self) -> bool:
        """Whether the feed says whose board this is (Lever and Ashby feeds do not)."""
        return bool(self.company_name or self.company_site)


class BoardError(Exception):
    """A board exists or was detected but its feed could not be read."""


class Adapter(Protocol):
    """One hiring system. Nothing outside an adapter knows that system's URLs or payloads."""

    system: str

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        """The company's listing, or None when the system has no board under this slug."""
        ...

    def guessable(self, slug: str) -> bool:
        """Whether a slug guessed from a domain is a valid key on this system at all."""
        ...

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        """One job's current content; None when it is no longer on the board."""
        ...


def job_from_listing(adapter: Adapter, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
    """For systems with no single-job endpoint: read the whole listing and pick the job."""
    listing = adapter.fetch(fetcher, ref)
    if listing is None:
        return None
    return next((j for j in listing.jobs if j.id == job_id), None)


def read_feed(response: Response, *, missing: tuple[int, ...] = (404,)) -> str | None:
    """The body of a feed response; None when the status means 'no such board'."""
    if response.status in missing:
        return None
    if not response.ok:
        raise BoardError(f"{response.url} answered {response.status}")
    return response.text
