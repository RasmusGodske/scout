from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from scout.modules import metering

log = logging.getLogger(__name__)

_RETRY = {429, 500, 502, 503, 529}


@dataclass(frozen=True)
class JevConfig:
    model: str
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    api_key_env: str = "TYPESAFE_API_KEY"
    usd_per_million_input_tokens: float = 0.042
    daily_spend_cap_usd: float = 5.0
    usage_log: str = "data/jev-usage.jsonl"
    max_questions_per_request: int = 100
    max_choice_options: int = 255
    timeout_seconds: float = 30.0
    retries: int = 3

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> JevConfig:
        known = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in section.items() if k in known})
        if cfg.model.endswith(("-latest", "-preview")) or cfg.model.startswith("~"):
            raise ValueError(f"pin a Jev version in scout.toml, not the alias {cfg.model!r}")
        return cfg


@dataclass(frozen=True)
class Choice:
    choice: str
    probabilities: dict[str, float]
    confidence: float


class JevUnavailable(Exception):
    """No API key, or today's spend cap is reached. Callers fall back to plain code."""


class JevError(Exception):
    """TypeSafe refused or failed the request."""


class Jev:
    """TypeSafe's Jev, reduced to the two judgments scout needs: a choice and yes/no batches.

    Every call logs the model that answered, latency and tokens to the usage log, and is
    refused once today's spend (UTC) would pass the cap.
    """

    def __init__(
        self,
        config: JevConfig,
        api_key: str | None,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._config = config
        self._key = api_key or None
        self._now = now
        self._client = httpx.Client(timeout=config.timeout_seconds, transport=transport)
        self._lock = threading.Lock()
        self._day, self._spent = self._spent_today()

    @property
    def available(self) -> bool:
        return self._key is not None and self._remaining() > 0

    @property
    def max_choice_options(self) -> int:
        return self._config.max_choice_options

    def choose(self, state: Any, instructions: Any, options: dict[str, Any], purpose: str) -> Choice:
        """One Choice question. `options` maps an option key to its description (or None)."""
        if not 2 <= len(options) <= self._config.max_choice_options:
            raise ValueError(
                f"a choice takes 2..{self._config.max_choice_options} options, got {len(options)}"
            )
        answers = self._ask(
            state,
            {"q": {"type": "choice", "instructions": instructions, "criteria": options}},
            purpose,
        )
        a = answers["q"]
        return Choice(
            a["choice"], {k: float(v) for k, v in a["probabilities"].items()}, float(a["confidence"])
        )

    def nouls(self, state: Any, questions: dict[str, Any], purpose: str) -> dict[str, float]:
        """Many yes/no questions over one state, batched; returns the probability of yes for each."""
        out: dict[str, float] = {}
        items = list(questions.items())
        size = self._config.max_questions_per_request
        for start in range(0, len(items), size):
            batch = {
                qid: {"type": "noul", "instructions": instr} for qid, instr in items[start : start + size]
            }
            answers = self._ask(state, batch, purpose)
            out.update({qid: float(a["noul"]) for qid, a in answers.items()})
        return out

    def close(self) -> None:
        self._client.close()

    # --- internal -----------------------------------------------------------------------------

    def _ask(self, state: Any, questions: dict[str, Any], purpose: str) -> dict[str, Any]:
        if self._key is None:
            raise JevUnavailable(f"no {self._config.api_key_env}")
        if self._remaining() <= 0:
            raise JevUnavailable(f"daily Jev spend cap of ${self._config.daily_spend_cap_usd} reached")
        meter = metering.current()
        if meter is not None:
            meter.before_jev()
        body = {"state": state, "model": self._config.model, "questions": questions}
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                r = self._client.post(
                    self._config.endpoint,
                    json=body,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
            except httpx.TransportError as exc:
                if attempt >= self._config.retries:
                    raise JevError(f"TypeSafe unreachable: {exc}") from exc
            else:
                if r.status_code not in _RETRY or attempt >= self._config.retries:
                    break
            attempt += 1
            time.sleep(min(2**attempt, 20))
        latency_ms = int((time.monotonic() - started) * 1000)
        if r.status_code != 200:
            raise JevError(f"TypeSafe answered {r.status_code}: {r.text[:300]}")
        data = r.json()
        usage = data.get("usage") or {}
        usd = self._record(data.get("model", ""), latency_ms, usage, len(questions), purpose)
        if meter is not None:
            meter.jev(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), usd)
        return data["answers"]

    def _record(self, model: str, latency_ms: int, usage: dict[str, Any], n: int, purpose: str) -> float:
        tokens_in = int(usage.get("input_tokens") or 0)
        if usage.get("cost") is not None:  # OpenRouter reports what it charged
            usd = float(usage["cost"])
        else:
            usd = tokens_in * self._config.usd_per_million_input_tokens / 1_000_000
        now = self._now()
        entry = {
            "at": now.isoformat(timespec="seconds"),
            "model": model,
            "latency_ms": latency_ms,
            "input_tokens": tokens_in,
            "output_tokens": int(usage.get("output_tokens") or 0),
            "usd": round(usd, 8),
            "questions": n,
            "purpose": purpose,
        }
        log.info("jev %s %s %dms %d tokens", purpose, model, latency_ms, tokens_in)
        if model and model != self._config.model:
            log.warning("asked for %s but %s answered", self._config.model, model)
        with self._lock:
            self._roll_day()
            self._spent += usd
            path = Path(self._config.usage_log)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        return usd

    def _remaining(self) -> float:
        with self._lock:
            self._roll_day()
            return self._config.daily_spend_cap_usd - self._spent

    def _roll_day(self) -> None:
        today = self._now().date().isoformat()
        if today != self._day:
            self._day, self._spent = today, 0.0

    def _spent_today(self) -> tuple[str, float]:
        today = self._now().date().isoformat()
        path = Path(self._config.usage_log)
        if not path.exists():
            return today, 0.0
        total = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(entry.get("at", "")).startswith(today):
                total += float(entry.get("usd") or 0)
        return today, total
