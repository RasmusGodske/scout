# AGENTS.md

Start here if you are a developer or a coding agent working on scout. `README.md` says what scout
does for its users; this file says how to work on it without breaking what it gets right.

## What scout is, and is not

scout is an HTTP API: a company's domain in, every job the company lists right now out, each with
a stable id, its raw content, locally extracted text, and optional structured details. It caches
each company's **route** (where its jobs live) so repeat scrapes are cheap, reports per-request
**usage** so a billing platform can be built on top, and keeps a **journal** of its decisions so
gaps can be found later.

Out of scope, by decision (2026-10-03):

- **Storing job listings.** The caller stores them; scout returns them and caches only routes.
  The caller passes `known_job_ids` to skip refetching jobs it already has.
- **Analysing ads** (HubSpot? commission? seniority?). A separate application reads the text.
  Jev is used *only* to find jobs, never to interpret them.
- **Users, accounts, credits, billing, an MCP server.** A future platform on top. scout measures
  usage; it never prices it. One service token (`SCOUT_API_TOKEN`) is all the auth it has.
- **Proxies or evading bot protection.** scout identifies itself, honours robots.txt and backs
  off. A site that blocks it is reported as `blocked`, not worked around.

Owner: Rasmus Godske (github.com/RasmusGodske/scout, private). It is not a Prowi project; do
not point code, user agents or repositories at Prowi.

## Getting started

```
poetry install && poetry run playwright install chromium    # the devcontainer does this
cp .env.example .env                                        # OPENROUTER_API_KEY=... for Jev
poetry run pytest                                           # ~140 tests, offline, ~2 s
poetry run scout scrape lunar.app                           # one real scrape
poetry run scout serve                                      # the API on :8000
```

Python 3.12, Poetry (not uv, not pip). Settings are in `scout.toml`; secrets only in `.env` and
environment variables. `data/` (route cache, journal, Jev usage log) is git-ignored.

## Where things are

`src/scout/modules/<module>/`, each with a `README.md` saying what it hides. **Read a module's
README before changing it.** `src/scout/app.py` wires everything from `scout.toml`;
`src/scout/api.py` (HTTP) and `src/scout/cli.py` are thin layers over `Scout`.

| Module | Owns |
|---|---|
| `scraper` | one request: route cache in, resolve, route cache and journal out, metering |
| `discovery` | the search order, following and replacing cached routes, the Jev judgments, `lookup_job` |
| `boards` | one adapter per hiring system (feeds, detection in HTML, slug guessing) |
| `pages` | pure HTML/URL functions: links, careers words, text, JSON-LD details, sitemaps |
| `fetch` | all crawling HTTP: per-host rate limits, robots.txt, retries, headless Chromium |
| `jev` | the only code that knows the Jev key and request shape; pinned model; spend cap |
| `routes` | SQLite route cache (`RouteCache.save` is the only writer) |
| `journal` | per-request decisions and miss snapshots; the gap report |
| `metering` | per-request usage and budgets |
| `benchmark` | judging results against `benchmark/labels.csv` |
| `results` | the shared data vocabulary (`ScrapeResult`, `Job`, `Problem`, ...) |

## Conventions (the design-principles binding for this repo)

The code follows [RasmusGodske/design-principles](https://github.com/RasmusGodske/design-principles);
review changes against it. How it maps onto Python here:

- **Public surface = `__init__.py` with `__all__`.** Files starting with `_` are internal to their
  module. Importing another module's `_internal` is a test failure
  (`tests/conventions/modules/no_module_reaches_into_another_test.py`).
- **Every module has a README** (also enforced) saying what it hides; update it with the code.
- **Pure core, impure shell.** `pages`, `boards/_detect`, adapters' `parse_*`, `benchmark.judge`
  and `journal.summarize` are pure: data in, data out, tested on saved inputs. Fetching, Jev
  and storage stay at the edges.
- **One writer per fact.** Routes: `RouteCache.save`. Journal: `Journal.record/snapshot`, called
  only from `scraper`. Jev usage log: `jev`. HTTP: `Fetcher`. Do not add a second path.
- **Config vs record.** Tunables (careers words, thresholds, host intervals, systems to guess)
  live in `scout.toml`, not in code. Results are records with stable ids.
- **Data minimization.** Never store or log a job's content or people's data: the journal keeps
  job ids, URLs and titles only; adapters drop contact persons (HR Manager, JOIN, Recruitee);
  saved test fixtures are scrubbed of emails and names.
- **No junk drawers** (`utils/`, `helpers/`, `common/`): enforced by a conventions test.
- **Explicit over clever.** Every job detail records its source (`board_feed`, `json_ld`,
  `page_heading`, `link_text`); every miss has a typed `Problem`.

## Tests

- `poetry run pytest` runs offline; a guard fails any test that opens a socket. Fake the web with
  `tests/support/sites.py` (`FakeWeb`, `jev_transport`). Live tests are `@pytest.mark.network`
  (`poetry run pytest -m network`).
- Tests mirror the source tree: `tests/src/scout/modules/<module>/`. Saved real responses live in
  that module's `support/`; when you save a new one, trim it and scrub emails and names.
- CI (`.github/workflows/image.yml`) runs ruff and the tests, then builds and pushes the image.
  Run `poetry run ruff check src tests benchmark && poetry run ruff format src tests benchmark`
  before committing.

## Measuring a change

Tests prove the code does what you meant; only the benchmark shows whether that is *better*.
For anything touching discovery, boards or the Jev questions, compare before and after on the
same day with `benchmark/compare.py`, following `benchmark/README.md`. Report companies right,
job precision, job recall, time and requests, and look at the per-company lines, not only the
totals: a total can hide a regression on one company.

Never tune on the benchmark's companies, and never add a customer's lead list to the repo
(not as labels, not as fixtures). For ad-hoc tests on real leads, keep files outside the repo.

## Finding what to fix next

The production journal is the source of truth for gaps:

```
poetry run scout report --remote https://<scout host> --token $SCOUT_API_TOKEN --since 30d
```

It ranks unknown hosts on careers pages (hiring systems without an adapter, by how many companies
use them), misses with request ids (`GET /v1/journal/snapshots/<id>/<page>` shows the page scout
saw), suspicious results, slug-guess outcomes and errors. Turn a miss into a test with the saved
snapshot, then fix.

## Adding a hiring system

Only when the journal or benchmark shows it matters. Then:

1. Verify the public feed live (status codes for a real and an unknown slug, the payload) and
   save a trimmed, scrubbed response to `tests/src/scout/modules/boards/support/`.
2. In `boards/_adapters.py`: a class with `system`, `guessable`, `fetch`, `fetch_job`, and a pure
   `parse_*` built on `_feed_job(...)` mapping the feed's own fields to `details` (title,
   location, posted_at, employment_type, department, salary). Drop personal fields.
   Register it in `ADAPTERS` and in `public_url`.
3. In `boards/_detect.py`: the URL patterns that reveal it on a page (and its asset host, if it
   can be served on a company's own domain).
4. Add it to `guess_systems` in `scout.toml` only if its feed names the company and guessing it
   pays off; add a `host_intervals` entry if its API is shared and fast (or rate-limits).
5. Document the endpoint and field mapping in `boards/README.md`, test, and run the benchmark.

## Lessons already paid for

- **Guessing boards from the domain mostly misses** and was half of all requests; it is the last
  resort, and a guessed board is trusted only if its feed names the company. Two companies can
  share a name ("Mayflower"), and companies leave stale boards behind (Molslinjen).
- **"job" can be a person's name** (Job van den Berk). Jev chooses the careers link even when one
  keyword matches.
- **Closed jobs often still have pages** ("Job not found"), as do staff profiles and product pages
  that look like ads. Every fetched job page is checked before it counts.
- **Workable rate-limits hard** (HTTP 429 to any guessing). Keep it out of `guess_systems`.
- **Some sites answer every path with the homepage**; such a "careers page" is rejected.
- **Jev: pin the model** (`typesafe/jev-1.13-20260917` through OpenRouter); aliases like
  `~typesafe/jev-latest` are refused. Changing it means re-running the benchmark.
- **`load_dotenv()` searches from the code's location**: code run from a worktree outside the
  project gets no Jev key and silently uses fallbacks. Export the key; check Jev cost is not $0.
- **Playwright is bound to its thread**: each worker thread gets its own browser and closes it.
- **The live route cache must survive upgrades**: add columns with a migration in
  `RouteCache._migrate`, never by assuming a fresh schema.

## Deploying

Pushes to `main` that pass CI publish `ghcr.io/rasmusgodske/scout:latest` (and `:sha-<commit>`;
tag `vX.Y.Z` for semver tags). Run it with `OPENROUTER_API_KEY`, `SCOUT_API_TOKEN` (without it the
API is open, for local use only) and a volume on `/app/data`. See README "Running it with
Docker".

## Commits

Small, explained commits on `main`, messages saying what changed and why (with numbers when a
change was measured). Do not commit `.env`, `data/`, lead lists, or downloaded pages: check
`git status` for stray files before `git add`.
