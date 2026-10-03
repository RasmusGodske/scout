# metering

Measures what one request consumed, so a platform built on scout can bill for it, and stops
a request that would pass its budget. scout measures; it never prices. The only money it
reports is Jev's, as OpenRouter charged it; HTTP requests, bytes, headless-browser renders and
wall time are reported as counts for the platform to price.

`with metered(budget) as meter:` opens a request's meter in a context variable. `fetch` and
`jev` report into `current()` and check the budget *before* each call, raising
`BudgetExceeded` instead of spending past it. Work fanned out to threads joins the same meter
when the thread runs in a copy of the request's context (`contextvars.copy_context()`), as
`boards.guess` does. Outside any request (`current()` is None) nothing is metered.

Public surface: `metered`, `current`, `Meter` (`usage()`), `Budget` (`max_usd`,
`max_http_requests`), `BudgetExceeded`. The `Usage` record itself is in `results`.
