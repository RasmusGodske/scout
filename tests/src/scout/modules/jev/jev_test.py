"""Tests for the Jev client: request shape, pinning, batching, usage log and the spend cap."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scout.modules.jev import Jev, JevConfig, JevUnavailable
from support.sites import jev_transport


def config(tmp_path: Path, **kw: object) -> JevConfig:
    base = {
        "model": "typesafe/jev-1.13-20260917",
        "endpoint": "https://jev.test/v1/systemone",
        "usage_log": str(tmp_path / "usage.jsonl"),
    }
    return JevConfig(**{**base, **kw})  # type: ignore[arg-type]


def test_aliases_are_refused() -> None:
    for alias in ("jev-latest", "~typesafe/jev-latest", "jev-preview"):
        with pytest.raises(ValueError):
            JevConfig.from_config({"model": alias})


def test_choose_sends_typesafe_shape_and_logs_usage(tmp_path: Path) -> None:
    seen: list[dict] = []
    jev = Jev(
        config(tmp_path),
        "key",
        transport=jev_transport(
            lambda q: {
                "type": "choice",
                "choice": "b",
                "probabilities": {"a": 0.1, "b": 0.9},
                "confidence": 0.8,
            },
            seen,
        ),
    )
    answer = jev.choose({"page": "x"}, "Which?", {"a": None, "b": "the b one"}, purpose="test")
    assert answer.choice == "b" and answer.probabilities["b"] == 0.9
    sent = seen[0]
    assert sent["model"] == "typesafe/jev-1.13-20260917" and sent["state"] == {"page": "x"}
    assert sent["questions"]["q"] == {
        "type": "choice",
        "instructions": "Which?",
        "criteria": {"a": None, "b": "the b one"},
    }
    entry = json.loads((tmp_path / "usage.jsonl").read_text().splitlines()[0])
    assert entry["model"] == "typesafe/jev-1.13-20260917" and entry["purpose"] == "test"
    assert entry["usd"] == 0.000004 and entry["input_tokens"] == 100 and "latency_ms" in entry


def test_nouls_are_batched(tmp_path: Path) -> None:
    seen: list[dict] = []
    jev = Jev(
        config(tmp_path, max_questions_per_request=2),
        "key",
        transport=jev_transport(lambda q: {"type": "noul", "noul": 0.7}, seen),
    )
    out = jev.nouls("state", {f"l{i}": f"q{i}" for i in range(5)}, purpose="test")
    assert out == {f"l{i}": 0.7 for i in range(5)}
    assert [len(r["questions"]) for r in seen] == [2, 2, 1]


def test_no_key_and_spend_cap_make_it_unavailable(tmp_path: Path) -> None:
    assert not Jev(config(tmp_path), None).available
    today = datetime.now(UTC).isoformat(timespec="seconds")
    (tmp_path / "usage.jsonl").write_text(json.dumps({"at": today, "usd": 5.0}) + "\n")
    jev = Jev(config(tmp_path, daily_spend_cap_usd=5.0), "key", transport=jev_transport(lambda q: {}))
    assert not jev.available
    with pytest.raises(JevUnavailable):
        jev.nouls("s", {"a": "q"}, purpose="test")


def test_choice_option_limit(tmp_path: Path) -> None:
    jev = Jev(config(tmp_path), "key", transport=jev_transport(lambda q: {}))
    with pytest.raises(ValueError):
        jev.choose("s", "q", {str(i): None for i in range(256)}, purpose="test")
