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

**Discovery**, the company's own site first, guessing last:

1. **Homepage**: fetch it (https, then www, then http). A hiring-system board it links to wins.
2. **Careers link**: with Jev, one Choice over the links with a careers word (in about a dozen
   languages, `scout.toml`) followed by the site's other links, with a `none` option. Even a
   lone keyword match is only a candidate: "job" can be a person's name. Without Jev, a lone
   keyword match is taken.
3. **Sitemap**: `/sitemap.xml` (and up to three child sitemaps) for URLs with a careers word,
   Jev choosing among several. One request instead of a dozen path guesses.
4. **Common paths** (`/careers`, `/karriere`, ...). A path that answers with the homepage itself
   (same title, near-same size) is not a careers page.
5. **The careers page**: a hiring-system board behind it (a redirect, an embed, or links to
   one), else Jev answers one yes/no per link ("a single job ad?"), batched, and each yes is
   fetched. A page with no job links is rendered once in the headless browser; if no real job
   ads remain, the link to the full job list is followed one hop (also off-site, e.g. a parent
   company's site).
   If still no jobs, and the page links to a job site scout does not read
   (`unsupported_job_sites` in `scout.toml`, LinkedIn's company job pages), the result is
   `jobs_on_unsupported_site` with that link as `job_list_url`, and no board is guessed: the
   company has said where its jobs are.
6. **Last resort, guess the board** from slugs derived from the domain (`boards.guess`), only
   when the site shows no jobs, and only a board whose feed names this company (Lever and Ashby
   feeds name nobody, and `lunar` on Ashby is not lunar.app). Guessing costs dozens of requests
   and rarely hits, which is why it runs last.

**Remembered with the route**: whether the careers page needed the headless browser (a rerun
renders only pages that need it), and that a site has no careers page (answered from the cache
until the route is older than `route_max_age_days`, then checked again).

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
