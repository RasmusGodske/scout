# scout

Give scout a company's domain; it gives back every job the company lists right now, each with a
stable id and its content. The first time, scout works out *how* to reach that company's jobs
(its **route**) and caches it, so later scrapes go straight there: cheaper and faster.

scout's job ends there. It does not store job listings and does not analyse them; the calling
application stores what it wants and asks its own questions of the ads.

## The API

```
poetry run scout serve --port 8000          # SCOUT_API_TOKEN=... to require a bearer token
```

| Call | Does |
|---|---|
| `POST /v1/scrape` `{"domain", "known_job_ids"?, "fresh"?, "include_raw"?}` | All jobs the company lists now |
| `POST /v1/jobs/lookup` `{"domain", "job_id", "include_raw"?}` | One job again: `listed` (with content), `not_listed` or `check_failed` |
| `GET /v1/routes/{domain}` | The cached route: where the jobs live, when it was last verified |
| `GET /healthz` | Liveness, and whether Jev is available |

Pass the ids you already have as `known_job_ids`: those jobs come back as references
(`"known": true`, no content) and their pages are not fetched again.

**Usage, for billing.** Every scrape and lookup answers with a `request_id` and a `usage`
block saying what that request consumed. scout measures and never prices: the only money it
reports is what Jev actually cost; the rest are counts a platform on top can price.

```jsonc
"request_id": "req_cded5cdbf9db480d8bc12799",
"usage": {
  "jev":  {"calls": 1, "input_tokens": 3744, "output_tokens": 20, "usd": 0.000157},
  "http": {"requests": 57, "rendered_pages": 0, "bytes": 4192000},  // renders are the costly kind
  "duration_ms": 20183
}
```

`route_source` tells a first scrape (`discovered`, `rediscovered`: the expensive kind) from a
cheap one (`cached`). Measured on pixelz.com: first scrape 57 requests, 1 Jev call ($0.000157),
20 s; the rerun 8 requests, no Jev, 7 s.

**Budget.** Pass `"budget": {"max_usd": 0.01, "max_http_requests": 100}` to stop a request
before it passes either. It answers `problem: budget_exceeded` with whatever was already paid
for: on a custom page the route is cached and the jobs fetched so far are returned. Requests
are reserved before they are sent, so the cap is exact even with parallel work; Jev is checked
before each call, so the last call can take spending slightly past `max_usd`.

The same operations from the command line (`scrape`, `lookup`, `route`, `batch` to JSON Lines,
`bench`) and as a Python library (`scout.app.Scout`).

## What a scrape returns

```jsonc
{
  "domain": "lunar.app",
  "status": "board",                 // board | page | no_careers_page | failed
  "careers_url": "https://lunar.teamtailor.com/jobs",
  "board": {"system": "teamtailor", "slug": "lunar"},   // null for a custom careers page
  "found_by": "slug_guess",          // how the route was first found
  "route_source": "cached",          // cached | discovered | rediscovered
  "rediscovered_because": null,      // route_failed | route_empty | route_stale
  "problem": null,                   // see below
  "jobs": [{
    "id": "513f2111-…",              // unique within the company, stable across scrapes
    "url": "https://jobs.lunar.app/jobs/8486641-…",
    "known": false,
    "content": {                     // the ad as fetched; null when "known"
      "format": "xml",               // json | xml (a hiring system's feed) | html (a job page)
      "raw": "<item>…</item>",       // untouched
      "text": "Banking hasn't changed…"   // extracted locally, no model
    },
    "details": {                     // MAY be filled: only what the site states plainly
      "title": "Accounts Payable/Receivable Specialist",
      "location": "København",
      "posted_at": "2026-10-01T13:32:55Z",
      "employment_type": null,
      "department": "Finance",
      "salary": null                 // {min, max, currency, period} when stated
    },
    "details_sources": {             // where each filled detail came from
      "title": "board_feed", "location": "board_feed",
      "posted_at": "board_feed", "department": "board_feed"
    }
  }]
}
```

**content vs details.** `content` is the ad itself: always there for a job you don't already
have. `details` are conveniences scout copies from structured data, never read or guessed from
the ad's prose, so any of them may be null. `details_sources` says where each came from:

| Source | Means |
|---|---|
| `board_feed` | the hiring system's own field (Greenhouse, Teamtailor, ...) |
| `json_ld` | the job page's schema.org `JobPosting` markup, the structured data Google for Jobs reads |
| `page_heading` | the job page's main heading (title only) |
| `link_text` | the text of the link that led to the job (title only) |

**When there are no jobs**, `problem` says why, as something code can branch on:

| `problem` | Meaning |
|---|---|
| `unreachable` | the site did not answer |
| `robots_disallowed` | the site's robots.txt asks bots like scout to stay out, and scout does |
| `blocked` | the site refuses bots (403, bot protection) |
| `needs_api_token` | the jobs are behind authentication (401) |
| `no_careers_page` | no careers page on the site |
| `no_jobs_found` | a careers page, but no job ads recognised on it (it may simply have none) |
| `job_list_unreadable` | the careers page links to a job list scout cannot read |
| `budget_exceeded` | the request's budget ran out; jobs may be partial |
| `error` | anything else; see `reason` |

## How it finds the jobs, and keeps finding them

**First scrape (discovery).** The company's own site first: a hiring-system board the homepage
links to; the careers link (Jev chooses, from links with careers words in about a dozen
languages and the site's other links); the sitemap; common paths. On the careers page, a hiring
system behind it, or Jev's batched yes/no per link ("a single job ad?") and then per fetched page
("an open position?"). Only when the site shows no jobs is a board guessed from the domain, and
only one whose feed names the company. Details: [`src/scout/modules/discovery`](src/scout/modules/discovery/README.md).

**Later scrapes** go straight along the cached route: one feed request for a board company, one
page fetch plus only new job pages for a custom-page company, and Jev only for links it hasn't
judged before.

**When a website changes**, scout notices and finds the new route (`route_source:
rediscovered`):

- the route **broke** (404, board deleted): rediscover at once (`route_failed`);
- it suddenly shows **no jobs** where it had some: rediscover to check (`route_empty`);
- it is **older than 30 days** and the homepage no longer leads to it, e.g. the company moved
  to a new hiring system and left the old board up: rediscover (`route_stale`). Costs one
  homepage request per company per month.

**Jev** (TypeSafe's decision model, through OpenRouter) is used only to find jobs: which
homepage link is the careers page, and which links are single job ads. About $0.0002 per
company on a first scrape; much less after.

## The journal: finding what to fix

Every scrape and lookup writes one line to a daily journal (`data/journal/YYYY-MM-DD.jsonl`):
the outcome, the route, usage, each job's id, URL and title (never its content), and the
decisions behind it: which slug guesses missed, how the careers link was chosen (keyword or Jev,
with Jev's probability), which paths were tried, how many links Jev judged as job ads, which
off-site hosts the careers page uses. For misses (`no_careers_page`, `no_jobs_found`,
`job_list_unreadable`) the homepage and careers page HTML are saved too, so the miss can be
reproduced. Entries are kept 90 days, snapshots 30.

```
poetry run scout report --since 30d                                        # this machine
poetry run scout report --remote https://scout.example.com --token $TOKEN  # a deployed scout
```

The report ranks the gaps: hiring systems with no adapter yet (off-site hosts by how many
companies use them), misses with their request ids, suspicious results ("job not found" or
repeated titles), slug-guess outcomes per system, problem counts, the slowest scrapes and
errors. The same is served as `GET /v1/journal/report`; the raw entries as `GET /v1/journal`
(JSON Lines, filter by `since`, `until`, `domain`, `problem`, `op`, `limit`) and a miss's pages
as `GET /v1/journal/snapshots/{request_id}/{name}`. All behind the bearer token.

## How well it works

Measured on 40 hand-labelled European companies that scout was never tuned on
([`benchmark/`](benchmark/README.md)), 2026-10-04:

| | |
|---|---|
| Companies right (right place, about the right number of jobs) | 88% |
| Job precision: reported jobs that are real open ads | 92% |
| Job recall: real open jobs found | 87% |
| First scrape | ~16 s per company (median), 16 in parallel; ~21 requests and ~$0.0003 of Jev per company |
| Repeat scrape on the cached route | ~5 s per company (median) |

The misses are mostly hiring systems scout has no adapter for yet, and pages for roles a company
is not currently hiring for that still look like job ads.

## Hiring systems

Greenhouse, Lever, Ashby, Workable, Recruitee, Teamtailor, HR Manager, Emply, JOIN and HiBob,
each through its public feed (endpoints and field mappings in
[`src/scout/modules/boards`](src/scout/modules/boards/README.md)). Any other site is read as a
custom careers page with Jev, so scout is not limited to these.

## Running it with Docker

```
docker build -t scout .
docker run -d -p 8000:8000 \
  -e OPENROUTER_API_KEY=sk-or-... \
  -e SCOUT_API_TOKEN=a-long-random-string \
  -v scout-data:/app/data \
  scout
curl -H "Authorization: Bearer a-long-random-string" -H "Content-Type: application/json" \
  -d '{"domain": "lunar.app"}' http://localhost:8000/v1/scrape
```

Or `docker compose up --build` with the keys in `.env` (see `docker-compose.yml`).

**Published image.** Every push to `main` that passes the tests is built and pushed to
`ghcr.io/rasmusgodske/scout:latest` (and `:sha-<commit>`); a tag `v1.2.0` also publishes `:1.2.0`
and `:1.2` (`.github/workflows/image.yml`). The package is private: log in first with
`echo $TOKEN | docker login ghcr.io -u RasmusGodske --password-stdin`, using a token with
`read:packages`, then `docker pull ghcr.io/rasmusgodske/scout:latest`.

| Variable | |
|---|---|
| `SCOUT_API_TOKEN` | Set: every `/v1` call needs `Authorization: Bearer <token>`. Unset: the API is open to anyone who can reach it (the log warns); for local or isolated networks only. `/healthz` is always open. |
| `OPENROUTER_API_KEY` | Jev, through OpenRouter. Without it scout runs on keyword and URL heuristics. |
| `SCOUT_CONFIG` | Settings file; default `/app/scout.toml`. Or mount your own over it. |

Mount a volume on `/app/data` to keep the route cache (and the Jev usage log) across restarts;
without it every new container rediscovers each company. The image runs as a non-root user,
includes Chromium's headless shell for JavaScript-only pages, and has a health check on
`/healthz`. `[api] max_concurrent_requests` (default 8) caps scrapes served at once; more wait.

## Setup

The devcontainer builds everything (Python 3.12, Poetry, Playwright's Chromium). By hand:

```
poetry install
poetry run playwright install chromium
cp .env.example .env    # OPENROUTER_API_KEY=... for Jev
```

Without a Jev key scout still runs, on keyword and URL heuristics, and says so.

Configuration is [`scout.toml`](scout.toml): careers words, common paths, Jev model (pinned),
thresholds and daily spend cap, route re-check age, rate limits per host, batch workers. The
route cache, the journal and the Jev usage log live in `data/` (not committed).

**Working on scout?** Read [`AGENTS.md`](AGENTS.md) first: scope, conventions, how to measure a
change, how to add a hiring system, and the lessons already learned.

## Tests

```
poetry run pytest              # no network; a guard fails any test that tries
poetry run pytest -m network   # live smoke test against real sites
```

Tests mirror the source tree under `tests/src/`; vendor feeds and pages saved on 2026-10-03
(trimmed, personal data scrubbed) live in each module's `support/`. `tests/conventions/`
checks module boundaries.

## Layout

`src/scout/modules/`, one directory per module, each with a README saying what it hides and an
`__init__.py` whose `__all__` is its public surface; `_`-prefixed files are internal.

| Module | Hides |
|---|---|
| `scraper` | the route cache and metering around each request; scrape, lookup, scrape many |
| `metering` | what one request consumed, and its budget |
| `discovery` | the search order, following and replacing cached routes, single-job lookup |
| `boards` | one adapter per hiring system; slug guessing; board detection in HTML |
| `pages` | links, careers-word scoring, job-link filtering, text, JSON-LD details (pure) |
| `fetch` | all crawling HTTP: rate limits, robots.txt, retries, the headless browser |
| `jev` | the Jev key and request shape, pinned model, usage log, daily spend cap |
| `routes` | the SQLite route cache and Jev link verdicts; no job listings |
| `journal` | every request's decisions and misses' pages, and the report of what to fix |
| `benchmark` | judging results against hand-checked labels |
| `results` | the shared vocabulary (`ScrapeResult`, `Job`, `JobLookup`, ...) |

`src/scout/app.py` wires them from `scout.toml`; `src/scout/api.py` is the HTTP API and
`src/scout/cli.py` the command line, both thin.
