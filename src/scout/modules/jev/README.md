# jev

The only code that knows the Jev API key and the shape of TypeSafe's `POST /v1/systemone`.
By default it goes through OpenRouter (`https://openrouter.ai/api/v1/systemone`, key in
`OPENROUTER_API_KEY`), which serves the same request and answer shape; TypeSafe directly works
by changing `endpoint`, `model` and `api_key_env` in `scout.toml`. It offers scout's two judgments: `choose` (one
Choice question, at most 255 options as TypeSafe documents) and `nouls` (many yes/no
questions over one state, batched up to `max_questions_per_request` per call, since TypeSafe
evaluates questions in a request in parallel).

What it hides: the request and answer shapes, retries on 429/529/5xx, the pinned model
(`scout.toml` refuses aliases such as `jev-latest` or OpenRouter's `~typesafe/jev-latest`;
pinned is `typesafe/jev-1.13-20260917`, the id OpenRouter reports for jev-1.13; a different
model answering is logged as a warning), and the daily spend cap. Every call appends one line to the usage log
(`data/jev-usage.jsonl`): the model that answered, latency, input/output tokens, dollars,
question count and purpose. Spend is OpenRouter's reported `usage.cost`
when present, else input tokens × `usd_per_million_input_tokens` ($0.042/Mtok for jev-1.13 on
2026-10-03); the cap is per UTC day and survives restarts by re-reading the log.

Each call is also reported to the current request's meter (`metering`), with the dollars
OpenRouter reported, and refused with `BudgetExceeded` if the request's `max_usd` is spent.

`JevUnavailable` means no key or the cap is reached; callers fall back to plain code.
`JevError` means TypeSafe refused the request.

Exception to "all HTTP goes through `fetch`": `fetch` exists for crawling politeness
(robots.txt, per-host rate limits). An authenticated API call has neither concern, so this
module talks to TypeSafe directly.
