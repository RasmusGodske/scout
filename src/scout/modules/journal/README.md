# journal

A record of what every request did and *why*, so that after scout has run against many
websites someone can find what to fix: hiring systems without an adapter, wrong careers pages,
results that look off, slug guesses that never pay off. The response tells the caller what
happened; the journal keeps the reasoning that is otherwise gone once the response is sent.

**Collecting.** `with tracing():` opens a request's trace in a context variable (like
`metering`); code anywhere in the request calls `note(kind, **fields)` to record a decision and
`keep_page(name, html)` to hold a page in memory in case the request ends as a miss. Threads
started with a copy of the request's context (as `boards.guess` does) add to the same trace.
Outside a request both are no-ops. Recorded today:

| kind | from | what |
|---|---|---|
| `slug_guess` | boards | per system and slug: `missing`, `empty`, `other_company`, `error`, `hit` |
| `homepage` | discovery | URL, rendered?, boards it links to, the slug-guessed board |
| `careers_link`, `listing_link` | discovery | how the link was chosen (`single_keyword`, `jev` with its probability, `keyword_fallback`, `no_links`) |
| `common_paths` | discovery | paths tried, the one that worked |
| `careers_page` | discovery | links, job-link candidates, Jev yes and borderline counts, rendered?, hop |
| `foreign_hosts` | discovery | off-site hosts the careers page links to, embeds or loads scripts from |
| `board_unreadable` | discovery | a board detected but not readable |
| `stale_check`, `rediscover` | discovery | whether an old route still holds, and why it was replaced |

**Keeping.** `Journal.record(entry)` appends one JSON line per request to
`<dir>/YYYY-MM-DD.jsonl`: outcome, route, usage, each job's id, URL and title (never its
content: it is large, already returned to the caller, and job pages can name people), and the
trace. For misses (`no_careers_page`, `no_jobs_found`, `job_list_unreadable`) the kept pages
are written to `<dir>/snapshots/YYYY-MM-DD/<request_id>/<name>.html`. Days older than
`retention_days` (90) and snapshots older than `snapshot_retention_days` (30) are deleted, once
a day on the next write. `scraper` is the only caller of `record` and `snapshot`; a failed
write is logged and never fails the request.

**Reading.** `entries(since, until, domain=, problem=, op=)`, `snapshot_names(request_id)`,
`snapshot_page(request_id, name)`, and `summarize(entries)`: a pure summary of the gaps
(unknown hosts ranked by how many companies use them, misses with their request ids,
suspicious results such as "job not found" titles or repeated titles, slug-guess outcomes per
system, problem and route counts, slowest scrapes, errors). Served as `GET /v1/journal`,
`/v1/journal/report` and `/v1/journal/snapshots/...`, and as `scout journal` / `scout report`
(local, or `--remote <url>` for a deployed scout).

Public surface: `Journal`, `JournalConfig`, `tracing`, `note`, `keep_page`, `Trace`, `summarize`.
