"""Scraping companies and looking up jobs, with the route cache kept current. See README.md.

Public surface: `Scraper`, `ScrapeRequest`, `read_domains`.
"""

from scout.modules.scraper._scraper import Scraper, ScrapeRequest, new_request_id, read_domains

__all__ = ["ScrapeRequest", "Scraper", "new_request_id", "read_domains"]
