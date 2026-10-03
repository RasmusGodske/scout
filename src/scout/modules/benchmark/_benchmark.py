from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from scout.modules.pages import canonical_url
from scout.modules.results import ScrapeResult, Status

NO_CAREERS = "no_careers_page"


@dataclass(frozen=True)
class Label:
    """One hand-checked domain. `system` is a hiring system (one scout has an adapter for, or
    another one such as `personio`), empty for a custom page on the company's own site, or
    `no_careers_page`. `job_urls` lists every open job's URL where the labeller recorded them
    (custom pages), which lets the benchmark measure precision and recall per job. Only labels
    with `checked_by` filled in count as checked."""

    domain: str
    careers_url: str
    system: str
    slug: str
    open_jobs: int | None
    labelled_by: str
    checked_by: str
    notes: str = ""
    job_urls: tuple[str, ...] = ()

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
    # Per-job counts, when the label lists job URLs or says there are none: real jobs found,
    # "jobs" found that are not real, and real jobs missed. None when not measurable.
    true_jobs: int | None = None
    false_jobs: int | None = None
    missed_jobs: int | None = None


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

    def job_precision(self) -> float | None:
        """Of the jobs scout reported (where measurable), the share that are real."""
        tp, fp = self._sum("true_jobs"), self._sum("false_jobs")
        return tp / (tp + fp) if tp + fp else None

    def job_recall(self) -> float | None:
        """Of the real jobs (where listed), the share scout found."""
        tp, fn = self._sum("true_jobs"), self._sum("missed_jobs")
        return tp / (tp + fn) if tp + fn else None

    def _sum(self, name: str) -> int:
        return sum(getattr(v, name) or 0 for v in self.verdicts)


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
                job_urls=tuple((r.get("job_urls") or "").split()),
            )
        )
    return out


def judge(label: Label, result: ScrapeResult, supported: frozenset[str] = frozenset()) -> Verdict:
    """Whether scout found what the label says. Pure.

    A hit needs the right kind of answer and, when the label counts jobs, a job count within
    max(2, 25%) of it. The right kind: the board for a hiring system scout supports
    (`supported`); for any other hiring system, or a custom page, the careers page itself
    (scout reads both as a page); no careers page when there is none.
    """
    misses: list[str] = []
    found = len(result.jobs)
    if label.system == NO_CAREERS:
        if result.status is not Status.NO_CAREERS_PAGE:
            misses.append(f"status: expected no careers page, got {result.status.value}")
    elif result.status is Status.FAILED:
        misses.append(f"failed: {result.reason}")
    elif label.system in supported:
        got = result.board.system if result.board else ""
        if got != label.system:
            misses.append(f"system: expected {label.system}, got {got or result.status.value}")
        elif label.slug and result.board and not _same_board(label, result.board.slug):
            misses.append(f"slug: expected {label.slug}, got {result.board.slug}")
    else:
        if result.status is not Status.PAGE:
            misses.append(f"status: expected a careers page, got {result.status.value}")
        elif label.careers_url and not _same_page(label.careers_url, result.careers_url):
            # A board on a hiring system scout has no adapter for is often embedded in the
            # company's own careers page; landing there, or reading jobs off the board's host,
            # is the right place too.
            board_host = urlsplit(label.careers_url).netloc.lower()
            on_board = label.system and any(urlsplit(j.url).netloc.lower() == board_host for j in result.jobs)
            # An empty board behind the company's own page: no jobs reported is the right answer.
            empty_board = label.system and label.open_jobs == 0 and not result.jobs
            if not (on_board or empty_board):
                misses.append(f"careers_url: expected {label.careers_url}, got {result.careers_url}")
    if label.system != NO_CAREERS and label.open_jobs is not None and not misses:
        tolerance = max(2, round(label.open_jobs * 0.25))
        if abs(found - label.open_jobs) > tolerance:
            misses.append(f"jobs: expected about {label.open_jobs}, got {found}")
    true_jobs = false_jobs = missed_jobs = None
    expected = _expected_jobs(label)
    if expected is not None:
        got = {_job_key(j.url) for j in result.jobs} | {_job_key(j.id) for j in result.jobs if "://" in j.id}
        real = {_job_key(u) for u in expected}
        found_real = {k for k in real if k in got}
        true_jobs = len(found_real)
        false_jobs = max(0, found - true_jobs)
        missed_jobs = len(real) - true_jobs
        if false_jobs and not misses:
            misses.append(f"false jobs: {false_jobs} reported jobs are not open job ads")
    return Verdict(
        label.domain,
        not misses,
        result.found_by.value if result.found_by else "",
        tuple(misses),
        found,
        label.open_jobs,
        true_jobs,
        false_jobs,
        missed_jobs,
    )


def _same_board(label: Label, slug: str) -> bool:
    """A board can be keyed by the system's slug (`hublo`) or by the company's own host it is
    served on (`careers.hublo.com`); either names the same board."""
    slug = slug.lower()
    return slug in (label.slug, urlsplit(label.careers_url).netloc.lower())


def _expected_jobs(label: Label) -> tuple[str, ...] | None:
    """The real job URLs, when the label makes them knowable: listed, or none open at all."""
    if label.job_urls:
        return label.job_urls
    if label.system == NO_CAREERS or label.open_jobs == 0:
        return ()
    return None


_LANG = re.compile(r"^/(da|en|de|fr|nl|sv|fi|no|nb|es|it|pl|pt|cs|dk)(?=/|$)", re.I)


def _job_key(url: str) -> str:
    """A job URL compared loosely: canonical, without a language prefix or `www.`."""
    canonical = canonical_url(url)
    parts = urlsplit(canonical)
    return (
        parts.netloc.removeprefix("www.")
        + _LANG.sub("", parts.path)
        + (f"?{parts.query}" if parts.query else "")
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
