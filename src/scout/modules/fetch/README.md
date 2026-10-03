# fetch

All HTTP goes through `Fetcher`, and nothing else in scout opens a connection. It hides five
things a caller would otherwise repeat at every call site: about one request per second per
host (across threads), `robots.txt` per origin (RFC 9309: a 4xx means no rules, a 5xx means
stay out; a host that doesn't answer at all raises `Unreachable`), retries with backoff on 429/5xx/connection errors, a size cap, and the
headless browser. `page(url)` is a GET that retries once in Chromium when the plain response
is near-empty (too little visible text or too few links, thresholds in `scout.toml`); most
sites never need it, so the browser starts only on first use.

Every request and render is reported to the current request's meter (`metering`) and
reserved against its budget before it is sent; robots.txt fetches and retries count too.

Public surface: `Fetcher`, `FetchPolicy`, `Response`, `FetchError`, `Blocked` (robots said no),
`Unreachable` (the host did not answer).

Known limits: redirects are followed by httpx, so `robots.txt` is checked for the first URL
only, not every hop. Each thread that renders starts its own Chromium (Playwright's sync API is
bound to its thread); a worker thread should call `close_thread()` before it exits.
