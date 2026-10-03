# routes

The cache that makes repeat scrapes cheap. For each company (keyed by domain) it keeps the
**route**: where its jobs live (a hiring-system board and slug, or a careers page URL), how
discovery found it, when it was last verified, and how many jobs it gave last time. It also
keeps Jev's verdict for every link judged on a careers page, so a rerun only asks Jev about
new links. It stores no job listings; those belong to the caller.

`RouteCache.save(result, now, verified=)` is the only writer. A failed scrape records its
problem but keeps the last good route, so the next scrape can try it again. `verified_at`
moves only when discovery found the route or the homepage confirmed it; the resolver uses it
to re-check routes older than `route_max_age_days`.

Public surface: `RouteCache` (`save`, `get`, `link_verdicts`), `CachedRoute`. Thread-safe.
Tables `routes` and `link_verdicts`, created on open; no migrations yet.
