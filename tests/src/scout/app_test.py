"""Live smoke tests against real sites. Run with `poetry run pytest -m network`."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout.app import Scout
from scout.modules.results import ListingStatus, Status

pytestmark = pytest.mark.network


def test_scrapes_a_board_and_looks_up_one_job(config_path: Path) -> None:
    with Scout.from_config(config_path, routes_path=":memory:") as scout:
        result = scout.scrape("lunar.app")
        assert result.status is Status.BOARD and result.board and result.board.system == "teamtailor"
        assert result.jobs
        assert result.jobs[0].details.posted_at and result.jobs[0].details_sources["title"] == "board_feed"
        lookup = scout.lookup_job("lunar.app", result.jobs[0].id)
        assert lookup.listing_status is ListingStatus.LISTED and lookup.job and lookup.job.content.text
