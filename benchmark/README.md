# benchmark

How we know whether scout is right, and whether a change made it better.

`labels.csv` holds 40 European companies, labelled by hand (by research agents fetching each
site directly, not by scout): the careers page, the hiring system if any, how many jobs were
open, and for custom careers pages the URL of every open job. None of them comes from a lead
list, and none was used while developing scout, so a score on them is honest. Keep it that way:
do not tune scout on these companies, and do not add companies from customers' lead lists.

## Current score (2026-10-04, commit 74751fc)

| | |
|---|---|
| Companies right | 35/40 (88%) |
| Job precision (reported jobs that are real) | 92% |
| Job recall (real jobs found) | 87% |
| First scrape of all 40, 16 at once | 98 s, 844 requests, $0.011 of Jev |
| Repeat scrape on cached routes | 32 s, 178 requests |

Known misses: hiring systems without an adapter yet (Welcome to the Jungle for dougs.fr;
variant.no's Recruitee board on its own host, which its homepage does not link); pages for
roles a company is *not* currently hiring for that still look like job ads (ackee.cz); one
soft page that serves the homepage under another URL (kaliber5.de).

## Scoring the current code

```
poetry run scout bench                    # all labels; prints drafted and checked rows separately
poetry run scout bench --checked-only     # only rows a person has verified
```

`bench` scrapes every labelled domain from scratch (`fresh`, in-memory route cache) and judges
each result with `judge()` in `src/scout/modules/benchmark`: the right kind of answer, a job
count within max(2, 25%), and per job (where the label lists job URLs) precision and recall.

## Comparing two versions of the code

To see whether a change helps, run both versions on the same day (jobs open and close) and
judge them with the same judge:

```
# 1. The labelled domains as a batch input.
python -c "import csv; rows=[r for r in csv.DictReader(l for l in open('benchmark/labels.csv') if not l.startswith('#'))]; open('/tmp/domains.csv','w').write('domain\n'+''.join(r['domain']+'\n' for r in rows))"

# 2. The old version in a worktree, run with this checkout's virtualenv.
git worktree add /tmp/scout-before <commit>
set -a; . ./.env; set +a        # <- needed: see the warning below
PYTHONPATH=/tmp/scout-before/src .venv/bin/python -m scout.cli --config /tmp/scout-before/scout.toml \
  --routes /tmp/before.sqlite batch /tmp/domains.csv --out /tmp/before.jsonl --fresh

# 3. The new version.
poetry run scout --routes /tmp/after.sqlite batch /tmp/domains.csv --out /tmp/after.jsonl --fresh

# 4. Side by side.
poetry run python benchmark/compare.py benchmark/labels.csv before /tmp/before.jsonl after /tmp/after.jsonl
```

**Warning:** `load_dotenv()` looks for `.env` upward from the *code's* location, so code run
from a worktree outside the project does not find the Jev key and silently runs on keyword
fallbacks. Export the key (step 2) or the "before" numbers are unfairly low; check that the
"before" Jev cost is not $0. Point both configs' `[journal] dir` and `[jev] usage_log` somewhere
temporary if you don't want the runs in `data/`.

## Labels

Columns: `domain, careers_url, system, slug, open_jobs, job_urls, labelled_by, checked_by,
notes`. `system` is a hiring system (those with an adapter are judged as a board; others such as
`personio` or `homerun` as a careers page), empty for a custom careers page, or
`no_careers_page`. `job_urls` is space-separated. Only rows with `checked_by` filled in count as
checked; the current rows are drafts (`labelled_by=claude-agents`) awaiting a spot-check.

Labels go stale as jobs open and close. Before trusting an old file, relabel `open_jobs` and
`job_urls`. The rules the labels were made with, for relabelling or adding companies:

- Verify by visiting the site (curl, and a rendered page where jobs load with JavaScript). Never
  run scout or read its code to label: the labels must be independent of what is judged.
- `careers_url` is the page that lists the jobs; for a hiring system, its board (and `slug`).
- Count the job ads open today. An open/unsolicited application counts only if it has its own
  ad page (say so in `notes`). Ads for roles marked "not hiring right now" do not count.
- On a custom careers page list every open job's URL in `job_urls`; follow pagination.
- Prefer small and mid-size B2B companies in several countries and languages, with a mix of
  hiring systems, custom pages, careers pages without openings, and no careers page at all.
- No company from a customer's lead list, and none scout was developed against.
