# scraper

scout's operations, as the API and CLI call them: `scrape(domain, known_job_ids)`,
`lookup_job(domain, job_id)` and `scrape_many(requests)`. It hides the route cache around each
scrape: it reads the cached route and link verdicts, decides whether the route is stale
(`route_max_age_days`, default 30), hands all of it to the resolver, and saves the route that
comes back. Job listings pass through to the caller; nothing about them is kept.

`known_job_ids` is how a caller that stores jobs keeps reruns cheap: those jobs are confirmed as
still listed and returned as references (`known: true`, no content) instead of being fetched.

Each `scrape` and `lookup_job` is one metered request (see `metering`): it gets a
`request_id`, a `usage` of everything it consumed (including a scrape a lookup had to run
first), and stops at its `budget`. The id and usage are logged at INFO.

`scrape_many` runs on a thread pool; each worker closes its headless browser when its scrape is
done. `read_domains` reads a CSV (`domain`/`website`/`url` column) or one domain per line.
