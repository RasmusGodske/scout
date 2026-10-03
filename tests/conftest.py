"""Suite-wide runtime guard: untagged tests may not touch the network.

A test that needs real websites or vendor APIs is marked `@pytest.mark.network` and only runs
with `pytest -m network`. Anything else that opens a socket fails at that moment, naming the
fix, so a "fast" test can't quietly become a slow, flaky one.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    if request.node.get_closest_marker("network"):
        return

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "this test tried to open a network connection; use httpx.MockTransport, "
            "or mark it @pytest.mark.network if it must reach the real site"
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def config_path() -> Path:
    return ROOT / "scout.toml"
