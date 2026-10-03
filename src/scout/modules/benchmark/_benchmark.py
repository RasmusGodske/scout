from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from scout.modules.results import ScrapeResult, Status

NO_CAREERS = "no_careers_page"


@dataclass(frozen=True)
class Label:
    """One hand-checked domain. `system` is a board system, empty for a custom page, or
    `no_careers_page`. Only labels with `checked_by` filled in count as checked."""

    domain: str
    careers_url: str
    system: str
    slug: str
    open_jobs: int | None
    labelled_by: str
    checked_by: str
    notes: str = ""

    @property
    def checked(self) -> bool:
        return bool(self.checked_by.strip())


@dataclass(frozen=True)
class Verdict:
    domain: str
    hit: bool
    found_by: str
    misses: tuple[str, ...]
    jobs_found: int
    jobs_expected: int | None


@dataclass
class Report:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return sum(v.hit for v in self.verdicts) / len(self.verdicts) if self.verdicts else 0.0

    def by_found_by(self) -> dict[str, tuple[int, int]]:
        """found_by -> (hits, total). Misses with no found_by are grouped under `-`."""
        out: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for v in self.verdicts:
            out[v.found_by or "-"][0] += v.hit
            out[v.found_by or "-"][1] += 1
        return {k: (h, t) for k, (h, t) in sorted(out.items())}

    def miss_reasons(self) -> Counter[str]:
        return Counter(m.split(":")[0] for v in self.verdicts for m in v.misses)


def read_labels(path: str | Path) -> list[Label]:
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    out = []
    for r in rows:
        jobs = (r.get("open_jobs") or "").strip()
        out.append(
            Label(
                domain=r["domain"].strip().lower(),
                careers_url=(r.get("careers_url") or "").strip(),
                system=(r.get("system") or "").strip().lower(),
                slug=(r.get("slug") or "").strip().lower(),
                open_jobs=int(jobs) if jobs.isdigit() else None,
                labelled_by=(r.get("labelled_by") or "").strip(),
                checked_by=(r.get("checked_by") or "").strip(),
                notes=(r.get("notes") or "").strip(),
            )
        )
    return out


def judge(label: Label, result: ScrapeResult) -> Verdict:
    """Whether scout found what the label says. Pure.

    A hit needs the right kind of answer (board system and slug, custom careers page, or no
    careers page) and, when the label counts jobs, a job count within max(2, 25%) of it.
    """
    misses: list[str] = []
    found = len(result.jobs)
    if label.system == NO_CAREERS:
        if result.status is not Status.NO_CAREERS_PAGE:
            misses.append(f"status: expected no careers page, got {result.status.value}")
    elif result.status is Status.FAILED:
        misses.append(f"failed: {result.reason}")
    elif label.system:
        got = result.board.system if result.board else ""
        if got != label.system:
            misses.append(f"system: expected {label.system}, got {got or result.status.value}")
        elif label.slug and result.board and result.board.slug.lower() != label.slug:
            misses.append(f"slug: expected {label.slug}, got {result.board.slug}")
    else:
        if result.status is not Status.PAGE:
            misses.append(f"status: expected a custom page, got {result.status.value}")
        elif label.careers_url and not _same_page(label.careers_url, result.careers_url):
            misses.append(f"careers_url: expected {label.careers_url}, got {result.careers_url}")
    if label.system != NO_CAREERS and label.open_jobs is not None and not misses:
        tolerance = max(2, round(label.open_jobs * 0.25))
        if abs(found - label.open_jobs) > tolerance:
            misses.append(f"jobs: expected about {label.open_jobs}, got {found}")
    return Verdict(
        label.domain,
        not misses,
        result.found_by.value if result.found_by else "",
        tuple(misses),
        found,
        label.open_jobs,
    )


def _same_page(a: str, b: str) -> bool:
    """Language variants are the same page, and so is a page and its parent on the same host
    (`careers.acme.com/` and `careers.acme.com/open-positions/`): both lead to the same list."""

    def key(url: str) -> tuple[str, str]:
        p = urlsplit(url if "://" in url else "https://" + url)
        path = p.path.rstrip("/").lower()
        for lang in ("/da", "/en", "/dk"):
            if path.startswith(lang + "/") or path == lang:
                path = path[len(lang) :]
        return p.netloc.lower().removeprefix("www."), path

    (host_a, path_a), (host_b, path_b) = key(a), key(b)
    if host_a != host_b:
        return False
    short, long = sorted((path_a, path_b), key=len)
    return long == short or long.startswith(short + "/")
