# results

The vocabulary every other module speaks, and nothing else: plain frozen data, no behaviour,
so `boards`, `pages`, `discovery`, `routes` and `scraper` pass results between them without
depending on each other. A contract module.

- `ScrapeResult`: one scrape of one company. The route (`status`, `careers_url`, `board`,
  `found_by`), where it came from (`route_source`: `cached` / `discovered` / `rediscovered`,
  with `rediscovered_because`), the `jobs`, and a typed `problem` when there are none for a
  reason other than "none open".
- `Job`: `id` (unique within its company and stable: the hiring system's own id, or the
  canonical job URL on a custom page), `url`, and three clearly separated parts:
  - `content` (`format`, `raw`, `text`): the ad as fetched, untouched, plus text extracted
    locally with no model. None when the caller said it already has the job (`known`).
  - `details` (`title`, `location`, `posted_at`, `employment_type`, `department`, `salary`):
    facts the site states in a structured way. Each MAY be filled; never read from prose.
  - `details_sources`: where each filled detail came from (`DetailSource`): `board_feed`,
    `json_ld`, `page_heading` or `link_text`.
- `JobLookup`: one job again by company and job id; `listing_status` is `listed`,
  `not_listed` or `check_failed`. It says whether the site still lists the job, nothing more.
