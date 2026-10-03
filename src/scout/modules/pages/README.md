# pages

Pure functions over HTML and URLs, no I/O. It hides the messy parts of reading a custom
careers page: resolving relative links, `<base>`, fragments, mail/phone/script links, files,
social links and language variants (`/da/karriere` and `/en/careers` are one candidate);
scoring links by careers words in their text or URL; narrowing a careers page's links to those
that could be a single job ad; turning a job page or feed fragment into readable text
(trafilatura, with a plain-text fallback for fragments it rejects); and reading the details a
job page states plainly.

**Details from a page** (`details_from_page`): per field, the first place that states it:
the page's schema.org `JobPosting` JSON-LD (the markup Google for Jobs reads: `title`,
`datePosted`, `employmentType`, `jobLocation`, `baseSalary`), then for the title only the page
heading, then the link text. JSON-LD field names are the standard's, so one mapping serves
every site; the standard's shape variations (one place or a list, a string or a list, a
posting at the top, in a list or under `@graph`) are handled here once. Broken blocks are
skipped. `normalize_date` turns ISO, epoch-millisecond, .NET `/Date(ms)/` and RFC 822 dates
into ISO 8601 and returns None rather than guess.

Public surface: `collect_links`, `careers_candidates`, `careers_score`, `job_link_candidates`,
`job_link_heuristic` (only used when Jev is unavailable), `canonical_url`, `extract_text`,
`page_title`, `details_from_page`, `find_job_posting`, `job_posting_fields`, `normalize_date`,
`same_site`, and the `Link` value.

Tests run on saved pages in `tests/src/scout/modules/pages/support/`.
