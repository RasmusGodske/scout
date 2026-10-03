# discovery

`Resolver.resolve(domain, known)` turns one domain into one `ScrapeResult` and writes nothing;
`scraper` decides what to cache. It hides the whole search order, every fallback, and when a
cached route can no longer be trusted.

**With a cached route** (`Known.previous`) it goes straight there, and rediscovers when:

| `rediscovered_because` | When |
|---|---|
| `route_failed` | the route broke: board gone, careers page 404/410, or it now redirects to the homepage |
| `route_empty` | the route shows no jobs where it showed some last time |
| `route_stale` | the route is older than `route_max_age_days` and the homepage no longer links to it (its board, the board's host, or the careers page or a page above it) |

If rediscovery leads back to the same route, it stands (`route_source: cached`). During a
`route_stale` rediscovery the old board cannot win back by slug guess (it may be the abandoned
one, like Molslinjen's old Teamtailor board); it still counts if the site's own links lead to it.

**Discovery**, cheapest first:

1. **Guess the board** from slugs derived from the domain (`boards.guess`), then weigh it
   against the homepage: a board the homepage links to wins outright. A guessed board that names
   this company but that the homepage does not link to is only a **fallback**, used when the
   company's own careers page shows no jobs or does not exist: it may be another company with
   the same name, or a secondary board next to the real list.
2. **Homepage link**: with Jev, one Choice over the links with a careers word (in about a dozen
   languages, `scout.toml`) followed by the site's other links, with a `none` option. Even a
   lone keyword match is only a candidate: "job" can be a person's name. Without Jev, a lone
   keyword match is taken.
3. **Common paths** (`/careers`, `/karriere`, ...) when the homepage gave nothing. A path that
   answers with the homepage itself (same title, near-same size) is not a careers page.
4. **Board behind the careers page**: a redirect to a board, an embed, or links to one.
5. **Custom page**: ask Jev one yes/no per link ("a single job ad?"), batched; fetch each yes.
   A page with no job links is rendered once in the headless browser, then followed one hop
   to its "open positions" list if needed (also off-site, e.g. a parent company's site).

**Checking job pages.** Every newly fetched job page goes through one batched Jev yes/no
("is this an ad for one specific position that is open now?", internships and apprenticeships
included) before it counts. Pages saying the job was not found or has closed, staff profiles,
product and service pages and articles are dropped; their titles and Jev's probabilities go to
the journal. Without Jev, only pages whose title says the job is gone are dropped. The same
check makes `lookup_job` answer `not_listed` for a page that still answers but says the job is
gone. Two links landing on the same job page give one job.

**Known jobs** (`Known.job_ids`, from the caller) are confirmed as still listed and returned
with `known: true` and no content; on a custom page their pages are not fetched. Cached
`link_verdicts` are not sent to Jev again; the result carries only new verdicts.

**Details** on custom pages come from `pages.details_from_page` (JSON-LD, then heading, then
link text); on boards from the adapter's feed mapping.

`lookup_job(route, job_id)` checks one job: the vendor's single-job endpoint or its list on a
board; on a page, the job URL (404/410 or a redirect elsewhere means `not_listed`).

Without Jev (no key, or the spend cap reached) it falls back to the best keyword match and a
URL heuristic, and logs that it did.
