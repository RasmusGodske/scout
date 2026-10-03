from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from scout.modules.boards._adapters import ADAPTERS, public_url
from scout.modules.boards._framework import BoardError, Listing
from scout.modules.boards._slugs import name_matches, slug_candidates
from scout.modules.fetch import Blocked, Fetcher, FetchError
from scout.modules.journal import note
from scout.modules.pages import same_site
from scout.modules.results import BoardRef, Job

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BoardsConfig:
    guess_systems: list[str] = field(default_factory=lambda: list(ADAPTERS))
    slug_suffixes: list[str] = field(default_factory=lambda: ["aps"])
    require_name_match: bool = True

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> BoardsConfig:
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in section.items() if k in known})
        unknown = [s for s in cfg.guess_systems if s not in ADAPTERS]
        if unknown:
            raise ValueError(f"no adapter for {unknown}; known: {sorted(ADAPTERS)}")
        return cfg


class Boards:
    """Reads a company's jobs from a hiring system's public feed."""

    def __init__(self, fetcher: Fetcher, config: BoardsConfig) -> None:
        self._fetcher = fetcher
        self._config = config

    @staticmethod
    def systems() -> list[str]:
        return sorted(ADAPTERS)

    @staticmethod
    def public_url(ref: BoardRef) -> str:
        """The board page a person would open; empty when the system has none we know."""
        return public_url(ref)

    def fetch(self, ref: BoardRef) -> Listing | None:
        """The listing for a known board; None when the system has no board under that slug.

        Raises BoardError when the feed exists but cannot be read, or the network fails.
        """
        adapter = ADAPTERS.get(ref.system)
        if adapter is None:
            raise BoardError(f"no adapter for {ref.system}")
        try:
            return adapter.fetch(self._fetcher, ref)
        except Blocked as exc:
            raise BoardError(str(exc)) from exc
        except FetchError as exc:
            raise BoardError(str(exc)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise BoardError(f"{ref.system} {ref.slug}: unreadable feed: {exc}") from exc

    def fetch_job(self, ref: BoardRef, job_id: str) -> Job | None:
        """One job's current content from its board; None when it has been taken down."""
        adapter = ADAPTERS.get(ref.system)
        if adapter is None:
            raise BoardError(f"no adapter for {ref.system}")
        try:
            return adapter.fetch_job(self._fetcher, ref, job_id)
        except FetchError as exc:
            raise BoardError(str(exc)) from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise BoardError(f"{ref.system} {ref.slug}: unreadable job {job_id}: {exc}") from exc

    def guess(self, domain: str, company_name: str = "") -> Listing | None:
        """Try each known system under slugs derived from the domain; the first with jobs wins.

        Systems are probed in parallel, slugs within one system in order, so each vendor sees
        at most one request per second from us. A board that names its company must match
        the domain, so a common word as a slug does not claim someone else's board. A board
        that names no company (`identifies_company` False) is only a lead: the caller should
        confirm it, e.g. by the company's own site linking to it.
        """
        slugs = slug_candidates(domain, self._config.slug_suffixes, company_name)

        def probe(system: str) -> Listing | None:
            adapter = ADAPTERS[system]
            for slug in slugs:
                if not adapter.guessable(slug):
                    continue
                try:
                    listing = adapter.fetch(self._fetcher, BoardRef(system, slug))
                except (BoardError, FetchError, ValueError, KeyError, TypeError) as exc:
                    log.debug("guess %s/%s: %s", system, slug, exc)
                    note("slug_guess", system=system, slug=slug, outcome="error", error=str(exc)[:200])
                    continue
                if listing is None or not listing.jobs:
                    note(
                        "slug_guess",
                        system=system,
                        slug=slug,
                        outcome="missing" if listing is None else "empty",
                    )
                    continue
                if self._config.require_name_match and not _belongs(domain, listing):
                    log.info("guess %s/%s rejected: board is %r", system, slug, listing.company_name)
                    note(
                        "slug_guess",
                        system=system,
                        slug=slug,
                        outcome="other_company",
                        board_names=listing.company_name or listing.company_site,
                    )
                    continue
                note(
                    "slug_guess",
                    system=system,
                    slug=slug,
                    outcome="hit",
                    jobs=len(listing.jobs),
                    names_company=listing.identifies_company,
                )
                return listing
            return None

        systems = self._config.guess_systems
        with ThreadPoolExecutor(max_workers=max(1, len(systems))) as pool:
            # Each probe runs in a copy of this context, so it reports to the request's meter.
            futures = [pool.submit(contextvars.copy_context().run, probe, system) for system in systems]
            results = [r for r in (f.result() for f in futures) if r is not None]
        # A board whose feed names this company beats one whose feed names no company at all;
        # within each kind, the configured order wins.
        named = [r for r in results if r.identifies_company]
        return (named or results or [None])[0]


def _belongs(domain: str, listing: Listing) -> bool:
    """A board that names its company's website must name this domain; one that names only
    the company must match the domain stem; one that names neither is accepted."""
    if listing.company_site:
        return same_site(listing.company_site, f"https://{domain}")
    if listing.company_name:
        return name_matches(domain, listing.company_name)
    return True
