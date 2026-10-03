from __future__ import annotations

import json
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True)
class JournalConfig:
    dir: str = "data/journal"
    retention_days: int = 90
    snapshots: bool = True
    snapshot_retention_days: int = 30

    @classmethod
    def from_config(cls, section: dict[str, Any]) -> JournalConfig:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in section.items() if k in known})


class Journal:
    """Append-only daily JSON Lines files, plus HTML snapshots of misses, each pruned by age.

    `record` and `snapshot` are the only writers. Thread-safe.
    """

    def __init__(
        self, config: JournalConfig, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._config = config
        self._root = Path(config.dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._pruned_on: date | None = None

    @property
    def snapshots_enabled(self) -> bool:
        return self._config.snapshots

    def record(self, entry: dict[str, Any]) -> None:
        now = self._clock()
        line = json.dumps({"at": now.isoformat(timespec="seconds"), **entry}, ensure_ascii=False, default=str)
        with self._lock:
            self._prune_daily(now.date())
            self._root.mkdir(parents=True, exist_ok=True)
            with (self._root / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8") as f:
                f.write(line + "\n")

    def snapshot(self, request_id: str, pages: dict[str, str]) -> list[str]:
        """Save pages (name -> HTML) for one request; returns the names saved."""
        if not self._config.snapshots or not pages or not _SAFE.match(request_id):
            return []
        folder = self._root / "snapshots" / self._clock().date().isoformat() / request_id
        with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            for name, html in pages.items():
                if _SAFE.match(name):
                    (folder / f"{name}.html").write_text(html, encoding="utf-8")
                    saved.append(name)
        return saved

    def entries(
        self,
        since: date | None = None,
        until: date | None = None,
        *,
        domain: str | None = None,
        problem: str | None = None,
        op: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Entries oldest first, filtered by day range (inclusive), domain, problem and op."""
        if not self._root.is_dir():
            return
        for path in sorted(self._root.glob("*.jsonl")):
            day = path.stem
            if not _DAY.match(day):
                continue
            if (since and day < since.isoformat()) or (until and day > until.isoformat()):
                continue
            with path.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if domain and entry.get("domain") != domain:
                        continue
                    if problem and entry.get("problem") != problem:
                        continue
                    if op and entry.get("op") != op:
                        continue
                    yield entry

    def snapshot_names(self, request_id: str) -> list[str]:
        folder = self._find(request_id)
        return sorted(p.stem for p in folder.glob("*.html")) if folder else []

    def snapshot_page(self, request_id: str, name: str) -> str | None:
        folder = self._find(request_id)
        if folder is None or not _SAFE.match(name):
            return None
        path = folder / f"{name}.html"
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def prune(self) -> None:
        """Delete journal days older than retention_days and snapshots older than theirs."""
        today = self._clock().date()
        keep_from = today - timedelta(days=self._config.retention_days)
        snap_from = today - timedelta(days=self._config.snapshot_retention_days)
        if self._root.is_dir():
            for path in self._root.glob("*.jsonl"):
                if _DAY.match(path.stem) and path.stem < keep_from.isoformat():
                    path.unlink(missing_ok=True)
        snaps = self._root / "snapshots"
        if snaps.is_dir():
            for folder in snaps.iterdir():
                if _DAY.match(folder.name) and folder.name < snap_from.isoformat():
                    shutil.rmtree(folder, ignore_errors=True)

    def _prune_daily(self, today: date) -> None:
        if self._pruned_on != today:
            self._pruned_on = today
            self.prune()

    def _find(self, request_id: str) -> Path | None:
        if not _SAFE.match(request_id):
            return None
        snaps = self._root / "snapshots"
        if not snaps.is_dir():
            return None
        for day in sorted(snaps.iterdir(), reverse=True):
            folder = day / request_id
            if folder.is_dir():
                return folder
        return None
