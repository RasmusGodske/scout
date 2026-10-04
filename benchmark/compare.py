"""Judge saved scrape results against the benchmark labels, side by side.

    poetry run python benchmark/compare.py benchmark/labels.csv before before.jsonl after after.jsonl

Each results file is the JSON Lines output of `scout batch --fresh` over the labelled domains
(see benchmark/README.md for how to produce one per version of the code). Prints, per version:
companies right, job precision and recall, jobs found, Jev cost and requests; then one line per
company with each version's verdict.
"""

from __future__ import annotations

import json
import sys

from scout.modules.benchmark import Report, judge, read_labels
from scout.modules.boards import Boards
from scout.modules.results import BoardRef, FoundBy, Job, Problem, ScrapeResult, Status


def load(path: str) -> dict[str, tuple[ScrapeResult, dict]]:
    out = {}
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    for line in lines:
        d = json.loads(line)
        out[d["domain"]] = (
            ScrapeResult(
                d["domain"],
                Status(d["status"]),
                careers_url=d["careers_url"],
                board=BoardRef(**d["board"]) if d["board"] else None,
                found_by=FoundBy(d["found_by"]) if d["found_by"] else None,
                jobs=tuple(Job(j["id"], j["url"], None) for j in d["jobs"]),
                problem=Problem(d["problem"]) if d["problem"] else None,
                reason=d.get("reason", ""),
            ),
            d,
        )
    return out


def main(argv: list[str]) -> None:
    if len(argv) < 3 or len(argv) % 2 == 0:
        sys.exit(__doc__)
    labels = read_labels(argv[0])
    runs = {name: load(path) for name, path in zip(argv[1::2], argv[2::2], strict=True)}
    supported = frozenset(Boards.systems())

    verdicts = {}
    for name, results in runs.items():
        report = Report([judge(l, results[l.domain][0], supported) for l in labels if l.domain in results])
        verdicts[name] = {v.domain: v for v in report.verdicts}
        usd = sum(d["usage"]["jev"]["usd"] for _, d in results.values())
        requests = sum(d["usage"]["http"]["requests"] for _, d in results.values())
        precision, recall = report.job_precision(), report.job_recall()
        print(
            f"{name:10} companies right {sum(v.hit for v in report.verdicts)}/{len(report.verdicts)} "
            f"({report.hit_rate:.0%})  job precision {precision or 0:.0%}  recall {recall or 0:.0%}  "
            f"jobs found {sum(v.jobs_found for v in report.verdicts)}  Jev ${usd:.4f}  {requests} requests"
        )
        print(f"{'':10} miss reasons {dict(report.miss_reasons())}")
    print()
    for label in labels:
        cells = []
        for name in runs:
            v = verdicts[name].get(label.domain)
            if v is None:
                cells.append(f"{'-':42}")
                continue
            expected = v.jobs_expected if v.jobs_expected is not None else "?"
            false_jobs = f" fp{v.false_jobs}" if v.false_jobs else ""
            cells.append(
                f"{'OK  ' if v.hit else 'MISS'} {v.jobs_found:3}/{expected!s:<3}{false_jobs:5} {'; '.join(v.misses)[:55]:55}"
            )
        print(f"{label.domain:16} {(label.system or 'page'):18} " + " | ".join(cells))


if __name__ == "__main__":
    main(sys.argv[1:])
