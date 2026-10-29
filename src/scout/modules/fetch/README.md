# fetch

All HTTP goes through `Fetcher`, and nothing else in scout opens a connection. It hides five
things a caller would otherwise repeat at every call site: about one request per second per
host (across threads), `robots.txt` per origin (RFC 9309: a 4xx means no rules, a 5xx means
stay out; a host that doesn't answer at all raises `Unreachable`), retries with backoff on 429/5xx/connection errors, a size cap, and the
headless browser. `page(url)` is a GET that retries once in Chromium when the plain response
is near-empty (too little visible text or too few links, thresholds in `scout.toml`); most
sites never need it, so the browser starts only on first use.

**Incomplete certificate chains.** Some servers send their certificate without the intermediate
that links it to a trusted root; browsers fetch the missing one from the URL the certificate
names (AIA), Python does not. On that one error (`unable to get local issuer certificate`)
`Fetcher` does what browsers do, once per host: reads the server's certificate, downloads its
issuer (and that issuer's, up to three levels: Let's Encrypt's YR1 needs Root YR, cross-signed
by ISRG Root X1), adds them to its verification store and retries. The chain must still end at
a root in certifi's bundle; partial chains are refused, so a forged intermediate gets nowhere.
The hiring system Reqruiting (`*.reqruiting.com`) serves such a chain, so every company using
it had unreadable job pages (`_tls.py`).

Every request and render is reported to the current request's meter (`metering`) and
reserved against its budget before it is sent; robots.txt fetches and retries count too.

Public surface: `Fetcher`, `FetchPolicy`, `Response`, `FetchError`, `Blocked` (robots said no),
`Unreachable` (the host did not answer).

Known limits: redirects are followed by httpx, so `robots.txt` is checked for the first URL
only, not every hop. Each thread that renders starts its own Chromium (Playwright's sync API is
bound to its thread); a worker thread should call `close_thread()` before it exits.
