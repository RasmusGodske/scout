"""The composition root: builds every module from scout.toml and .env, and offers scout's
operations as one small library surface. The HTTP API and the CLI are thin layers over this."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from scout.modules.boards import Boards, BoardsConfig
from scout.modules.discovery import DiscoveryConfig, Resolver, normalize_domain
from scout.modules.fetch import Fetcher, FetchPolicy
from scout.modules.jev import Jev, JevConfig
from scout.modules.metering import Budget
from scout.modules.results import JobLookup, ScrapeResult
from scout.modules.routes import CachedRoute, RouteCache
from scout.modules.scraper import Scraper, ScrapeRequest


class Scout:
    """scout as a library.

    with Scout.from_config() as scout:
        result = scout.scrape("acme.dk")                          # ScrapeResult
        lookup = scout.lookup_job("acme.dk", result.jobs[0].id)   # JobLookup
    """

    def __init__(
        self, settings: dict[str, Any], *, api_key: str | None = None, routes_path: str | None = None
    ) -> None:
        """`api_key` overrides the Jev key read from the environment variable in scout.toml."""
        self.settings = settings
        self.fetcher = Fetcher(FetchPolicy.from_config(settings["fetch"]))
        self.boards = Boards(self.fetcher, BoardsConfig.from_config(settings.get("boards", {})))
        jev_config = JevConfig.from_config(settings["jev"])
        self.jev = Jev(jev_config, api_key if api_key is not None else os.environ.get(jev_config.api_key_env))
        discovery = settings["discovery"]
        self.resolver = Resolver(self.fetcher, self.boards, self.jev, DiscoveryConfig.from_config(discovery))
        self.routes = RouteCache(routes_path or settings["routes"]["path"])
        self.scraper = Scraper(
            self.resolver,
            self.fetcher,
            self.routes,
            route_max_age_days=float(discovery.get("route_max_age_days", 30)),
        )

    @classmethod
    def from_config(cls, path: str | Path = "scout.toml", *, routes_path: str | None = None) -> Scout:
        load_dotenv()
        settings = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls(settings, routes_path=routes_path)

    # --- operations ---------------------------------------------------------------------------

    def scrape(
        self,
        domain: str,
        known_job_ids: Iterable[str] = (),
        *,
        fresh: bool = False,
        budget: Budget | None = None,
    ) -> ScrapeResult:
        return self.scraper.scrape(domain, known_job_ids, fresh=fresh, budget=budget)

    def lookup_job(self, domain: str, job_id: str, *, budget: Budget | None = None) -> JobLookup:
        return self.scraper.lookup_job(domain, job_id, budget=budget)

    def scrape_many(
        self,
        requests: Iterable[ScrapeRequest],
        *,
        workers: int | None = None,
        on_result: Callable[[ScrapeResult], None] | None = None,
    ) -> list[ScrapeResult]:
        workers = workers or int(self.settings.get("batch", {}).get("workers", 8))
        return self.scraper.scrape_many(requests, workers=workers, on_result=on_result)

    def route(self, domain: str) -> CachedRoute | None:
        """The cached route for a company, if scout has scraped it before."""
        return self.routes.get(normalize_domain(domain))

    def close(self) -> None:
        self.fetcher.close()
        self.jev.close()
        self.routes.close()

    def __enter__(self) -> Scout:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
