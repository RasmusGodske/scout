"""Tests for the HTTP API: shapes, the bearer token, and that each endpoint is one Scout call.
Runs in-process with FastAPI's TestClient against a fake web."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scout.api import create_app
from scout.app import Scout
from scout.modules.fetch import Fetcher, FetchPolicy
from support.sites import FakeWeb

FILLER = "<a href=/n1>a</a><a href=/n2>b</a><a href=/n3>c</a><a href=/n4>d</a><a href=/n5>e</a>" + "x" * 600


@pytest.fixture
def client(config_path: Path, tmp_path: Path) -> TestClient:
    web = FakeWeb()
    web.html("https://acme.dk/", f"<a href='/karriere'>Karriere</a>{FILLER}")
    web.html("https://acme.dk/karriere", f"<a href='/karriere/kok-1'>Kok</a>{FILLER}")
    web.html("https://acme.dk/karriere/kok-1", f"<h1>Kok</h1><p>{'Mad. ' * 100}</p>")
    settings = tomllib.loads(config_path.read_text())
    settings["boards"]["guess_systems"] = []
    settings["jev"]["usage_log"] = str(tmp_path / "u.jsonl")
    settings["journal"]["dir"] = str(tmp_path / "journal")
    scout = Scout(settings, api_key="", routes_path=":memory:")
    fake = Fetcher(FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=web.transport())
    for holder in (scout.boards, scout.resolver, scout.scraper):
        holder._fetcher = fake  # noqa: SLF001 - point the whole graph at the fake web
    return TestClient(create_app(scout, token="secret"))


AUTH = {"Authorization": "Bearer secret"}


def test_scrape_lookup_and_route(client: TestClient) -> None:
    r = client.post("/v1/scrape", json={"domain": "acme.dk"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "page" and body["route_source"] == "discovered"
    job = body["jobs"][0]
    assert job["id"] == "https://acme.dk/karriere/kok-1"
    assert job["content"]["format"] == "html" and "Mad." in job["content"]["text"] and job["content"]["raw"]
    assert job["details"]["title"] == "Kok" and job["details_sources"] == {"title": "page_heading"}
    assert job["details"]["posted_at"] is None and job["known"] is False

    r = client.post(
        "/v1/scrape",
        json={"domain": "acme.dk", "known_job_ids": [job["id"]], "include_raw": False},
        headers=AUTH,
    )
    again = r.json()
    assert (
        again["route_source"] == "cached"
        and again["jobs"][0]["known"]
        and again["jobs"][0]["content"] is None
    )

    r = client.post("/v1/jobs/lookup", json={"domain": "acme.dk", "job_id": job["id"]}, headers=AUTH)
    assert r.json()["listing_status"] == "listed" and r.json()["job"]["details"]["title"] == "Kok"

    r = client.get("/v1/routes/acme.dk", headers=AUTH)
    assert r.status_code == 200 and r.json()["route"]["careers_url"] == "https://acme.dk/karriere"
    assert client.get("/v1/routes/unknown.dk", headers=AUTH).status_code == 404


def test_token_is_required(client: TestClient) -> None:
    assert client.post("/v1/scrape", json={"domain": "acme.dk"}).status_code == 401
    assert (
        client.post(
            "/v1/scrape", json={"domain": "acme.dk"}, headers={"Authorization": "Bearer no"}
        ).status_code
        == 401
    )
    assert client.get("/healthz").status_code == 200


def test_usage_and_budget_in_the_api(client: TestClient) -> None:
    body = client.post("/v1/scrape", json={"domain": "acme.dk"}, headers=AUTH).json()
    assert body["request_id"].startswith("req_")
    assert set(body["usage"]) == {"jev", "http", "duration_ms"}
    assert body["usage"]["http"]["requests"] > 0 and body["usage"]["jev"]["usd"] == 0
    capped = client.post(
        "/v1/scrape",
        json={"domain": "other.dk", "budget": {"max_http_requests": 0}},
        headers=AUTH,
    ).json()
    assert capped["problem"] == "budget_exceeded" and capped["usage"]["http"]["requests"] == 0
    lookup = client.post("/v1/jobs/lookup", json={"domain": "acme.dk", "job_id": "x"}, headers=AUTH).json()
    assert lookup["request_id"].startswith("req_") and "usage" in lookup
    assert (
        client.post(
            "/v1/scrape", json={"domain": "acme.dk", "budget": {"max_usd": -1}}, headers=AUTH
        ).status_code
        == 422
    )


def test_without_a_token_the_api_is_open(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCOUT_API_TOKEN", raising=False)
    settings = tomllib.loads(config_path.read_text())
    settings["jev"]["usage_log"] = str(tmp_path / "u.jsonl")
    settings["journal"]["dir"] = str(tmp_path / "journal")
    scout = Scout(settings, api_key="", routes_path=":memory:")
    open_client = TestClient(create_app(scout))
    assert open_client.get("/v1/routes/acme.dk").status_code == 404  # reached, not 401


def test_token_comes_from_the_environment(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCOUT_API_TOKEN", "from-env")
    settings = tomllib.loads(config_path.read_text())
    settings["jev"]["usage_log"] = str(tmp_path / "u.jsonl")
    settings["journal"]["dir"] = str(tmp_path / "journal")
    client = TestClient(create_app(Scout(settings, api_key="", routes_path=":memory:")))
    assert client.get("/v1/routes/acme.dk").status_code == 401
    assert client.get("/v1/routes/acme.dk", headers={"Authorization": "Bearer from-env"}).status_code == 404


def test_journal_endpoints(client: TestClient) -> None:
    client.post("/v1/scrape", json={"domain": "acme.dk"}, headers=AUTH)
    client.post("/v1/scrape", json={"domain": "nothing.dk"}, headers=AUTH)
    assert client.get("/v1/journal").status_code == 401
    lines = client.get("/v1/journal?since=1d", headers=AUTH).text.strip().splitlines()
    assert [json.loads(l)["domain"] for l in lines][-2:] == ["acme.dk", "nothing.dk"]
    one = client.get("/v1/journal?domain=acme.dk&limit=1", headers=AUTH).text.strip().splitlines()
    assert len(one) == 1
    report = client.get("/v1/journal/report", headers=AUTH).json()
    assert report["requests"]["scrapes"] >= 2 and "unknown_hosts" in report
    assert client.get("/v1/journal?since=yesterday", headers=AUTH).status_code == 422
    assert client.get("/v1/journal/snapshots/req_nope/homepage", headers=AUTH).status_code == 404
