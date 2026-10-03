# boards

One adapter per hiring system, each the only code that knows that vendor's URLs and payload
shape; everything else sees `BoardRef` in and `Listing` (a tuple of `Job`) out. On top of the
adapters it hides slug guessing (domain → `acme`, `acme-dk`, `acmeaps`, … tried against every
system in parallel, with a company-name check so `acme.dk` does not claim another Acme's
board) and board detection from a page's URL and HTML (links, embeds, iframes, scripts, and
Teamtailor/Emply sites served on the company's own host).

Public surface: `Boards.fetch(ref)`, `Boards.guess(domain)`, `detect_boards(html, url)`,
`slug_candidates`, `name_matches`, `Listing`, `BoardError`, `BoardsConfig`.

Adding a system: write a class with `system`, `guessable(slug)` and `fetch(fetcher, ref)` and
a pure `parse_*` in `_adapters.py`, register it in `ADAPTERS`, add its URL patterns to
`_detect.py`, save a real feed response under `tests/src/scout/modules/boards/support/`.

## Feeds, verified live on 2026-10-03

| System | Feed | Notes |
|---|---|---|
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true` | 404 for unknown slugs. `content` is entity-escaped HTML. EU-hosted boards (`job-boards.eu.greenhouse.io`) answer the US feed with an empty list and have no public feed we found; they are read as a custom page. |
| Lever | `GET api.lever.co/v0/postings/<slug>?mode=json` | `api.eu.lever.co` for EU accounts. No company name in the payload. |
| Ashby | `GET api.ashbyhq.com/posting-api/job-board/<slug>` | `isListed: false` jobs are skipped. No company name. |
| Workable | `GET apply.workable.com/api/v1/widget/accounts/<slug>?details=true` | Without `details=true` there is no description. |
| Recruitee | `GET <slug>.recruitee.com/api/offers` | The per-offer `mailbox_email` is dropped. |
| Teamtailor | `GET <host>/jobs.rss` | No JSON API without a key; the public RSS carries full HTML descriptions. Works on `<slug>.teamtailor.com` and on custom hosts (`jobs.lunar.app`). |
| HR Manager | `GET api.hr-manager.net/jobportal.svc/<alias>/positionlist/json/?incAds=1&take=&skip=` | `incAds=1` includes the ad text. An unknown alias answers 400 with `CustomerAlias: null`. Hiring-manager and project-member fields are dropped before storage. Pages that only link `candidate.hr-manager.net/...?cid=<n>` don't reveal the alias; they fall to the custom-page path. |
| Emply | `POST <host>/api/integration/vacancy/get-page` | The career site's jobs widget call; needs the widget's `sectionId`, read from the jobs page. Job URL is `<host>/<lang>/ad/<titleAsUrl>/<shortId>`. |
| JOIN | `GET join.com/companies/<slug>?page=N`, then each job page | Token-only API; the public company page embeds the job list (5 per page) and each job page the full ad (intro, tasks, requirements, benefits), as Next.js `__NEXT_DATA__`. The feed names the company's website, which must match the domain. Contact-person fields are dropped. |
| HiBob | `GET <slug>.careers.hibob.com/api/job-ad` with `Referer: <site>/jobs` | Answers 401 without the Referer. No company name. |

## Details each feed states

Copied from the feed's own fields (`details_sources` says `board_feed`), never inferred; a
field the feed leaves empty stays null. Dates are normalized to ISO 8601.

| System | posted_at | department | employment_type | salary |
|---|---|---|---|---|
| Greenhouse | `first_published` | `departments[].name` | - | - |
| Lever | `createdAt` (ms) | `categories.department` | `categories.commitment` | `salaryRange` |
| Ashby | `publishedAt` | `department` | `employmentType` | - |
| Workable | `published_on` | `department` | `employment_type` | - |
| Recruitee | `published_at` | `department` | `employment_type_code` | `salary` |
| Teamtailor | `<pubDate>` | `<tt:department>` | - | - |
| HR Manager | `Published` (`/Date(ms)/`) | `Department.Name` | `WorkHours` | - |
| Emply | `published` | `department` | - | - |
| JOIN | `createdAt` | - | `employmentType.name` | `salaryAmountFrom/To` (cents), only when `settings.showSalary` |
| HiBob | `publishedAt` | `department` | `employmentType` | `payTransparency*` |

Title and location come from each feed's title and location fields.

Single-job lookups (`Boards.fetch_job`): Greenhouse `/jobs/<id>`, Lever `/postings/<slug>/<id>`,
Recruitee `/api/offers/<id>` and JOIN `/companies/<slug>/<id>` answer 404 for a job that is
gone; the others have no single-job endpoint, so the job is picked from the full list.
