"""One adapter per hiring system. Each `parse_*` function is pure: feed payload in, jobs out.

Endpoints verified live on 2026-10-03; see README.md for the evidence and the gaps.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from urllib.parse import quote

from lxml import etree

from scout.modules.boards._framework import Adapter, BoardError, Listing, job_from_listing, read_feed
from scout.modules.fetch import Fetcher
from scout.modules.pages import extract_text, normalize_date
from scout.modules.results import BoardRef, DetailSource, Job, JobContent, JobDetails, Salary

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def _jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _plain(value: Any) -> str:
    return " ".join(str(value or "").split())


def _feed_job(
    *,
    id: str,
    url: str,
    raw: str,
    fmt: str,
    text: str | None,
    title: Any = None,
    location: Any = None,
    posted_at: Any = None,
    employment_type: Any = None,
    department: Any = None,
    salary: Salary | None = None,
) -> Job:
    """A job from a hiring system's feed. Every detail is the feed's own field, copied as is
    (dates normalized to ISO 8601); a field the feed leaves empty stays None."""
    values = {
        "title": _plain(title) or None,
        "location": _plain(location) or None,
        "posted_at": normalize_date(posted_at),
        "employment_type": _plain(employment_type) or None,
        "department": _plain(department) or None,
        "salary": salary,
    }
    details = JobDetails(**values)
    sources = {k: DetailSource.BOARD_FEED for k, v in values.items() if v is not None}
    return Job(
        id=id, url=url, content=JobContent(fmt, raw, text or ""), details=details, details_sources=sources
    )


def _salary(low: Any, high: Any, currency: Any, period: Any, divisor: float = 1) -> Salary | None:
    def number(v: Any) -> float | None:
        try:
            return float(v) / divisor if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    low_n, high_n = number(low), number(high)
    if low_n is None and high_n is None:
        return None
    return Salary(low_n, high_n, _plain(currency) or None, _plain(period).lower() or None)


# --- Greenhouse -------------------------------------------------------------------------------


class Greenhouse:
    system = "greenhouse"

    def guessable(self, slug: str) -> bool:
        return bool(_SLUG.match(slug))

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        if ref.extra == "eu":
            # The EU data centre's boards have no public JSON feed we could find; the US feed
            # answers them with an empty list. Discovery reads them as a page instead.
            return None
        url = f"https://boards-api.greenhouse.io/v1/boards/{quote(ref.slug)}/jobs?content=true"
        body = read_feed(fetcher.get(url, accept="application/json"))
        return None if body is None else parse_greenhouse(ref, body)

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        if ref.extra == "eu":
            return None
        url = f"https://boards-api.greenhouse.io/v1/boards/{quote(ref.slug)}/jobs/{quote(job_id)}"
        body = read_feed(fetcher.get(url, accept="application/json"))
        if body is None:
            return None
        jobs = parse_greenhouse(ref, json.dumps({"jobs": [json.loads(body)]})).jobs
        return jobs[0] if jobs else None


def parse_greenhouse(ref: BoardRef, body: str) -> Listing:
    jobs = json.loads(body).get("jobs") or []
    out = []
    name = ""
    for j in jobs:
        name = name or _plain(j.get("company_name"))
        content = html_lib.unescape(j.get("content") or "")
        out.append(
            _feed_job(
                id=str(j["id"]),
                title=_plain(j.get("title")),
                url=j.get("absolute_url") or "",
                location=(j.get("location") or {}).get("name"),
                posted_at=j.get("first_published"),
                department=", ".join(d.get("name", "") for d in j.get("departments") or []),
                raw=_jdump(j),
                fmt="json",
                text=extract_text(content),
            )
        )
    return Listing(ref, tuple(out), name)


# --- Lever ------------------------------------------------------------------------------------


class Lever:
    system = "lever"

    def guessable(self, slug: str) -> bool:
        return bool(_SLUG.match(slug))

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        host = "api.eu.lever.co" if ref.extra == "eu" else "api.lever.co"
        url = f"https://{host}/v0/postings/{quote(ref.slug)}?mode=json"
        body = read_feed(fetcher.get(url, accept="application/json"))
        return None if body is None else parse_lever(ref, body)

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        host = "api.eu.lever.co" if ref.extra == "eu" else "api.lever.co"
        url = f"https://{host}/v0/postings/{quote(ref.slug)}/{quote(job_id)}"
        body = read_feed(fetcher.get(url, accept="application/json"))
        if body is None:
            return None
        jobs = parse_lever(ref, json.dumps([json.loads(body)])).jobs
        return jobs[0] if jobs else None


def parse_lever(ref: BoardRef, body: str) -> Listing:
    data = json.loads(body)
    if not isinstance(data, list):
        raise BoardError(f"lever {ref.slug}: unexpected payload")
    out = []
    for j in data:
        parts = [j.get("description") or ""]
        for lst in j.get("lists") or []:
            parts.append(f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>")
        parts.append(j.get("additional") or "")
        out.append(
            _feed_job(
                id=str(j["id"]),
                title=_plain(j.get("text")),
                url=j.get("hostedUrl") or "",
                location=(j.get("categories") or {}).get("location"),
                posted_at=j.get("createdAt"),
                employment_type=(j.get("categories") or {}).get("commitment"),
                department=(j.get("categories") or {}).get("department"),
                salary=_salary(
                    (j.get("salaryRange") or {}).get("min"),
                    (j.get("salaryRange") or {}).get("max"),
                    (j.get("salaryRange") or {}).get("currency"),
                    (j.get("salaryRange") or {}).get("interval"),
                ),
                raw=_jdump(j),
                fmt="json",
                text=extract_text("".join(parts)),
            )
        )
    return Listing(ref, tuple(out))


# --- Ashby ------------------------------------------------------------------------------------


class Ashby:
    system = "ashby"

    def guessable(self, slug: str) -> bool:
        return bool(_SLUG.match(slug))

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(ref.slug)}"
        body = read_feed(fetcher.get(url, accept="application/json"))
        return None if body is None else parse_ashby(ref, body)


def parse_ashby(ref: BoardRef, body: str) -> Listing:
    jobs = json.loads(body).get("jobs") or []
    out = []
    for j in jobs:
        if j.get("isListed") is False:
            continue
        text = j.get("descriptionPlain") or extract_text(j.get("descriptionHtml") or "")
        out.append(
            _feed_job(
                id=str(j["id"]),
                title=_plain(j.get("title")),
                url=j.get("jobUrl") or "",
                location=j.get("location"),
                posted_at=j.get("publishedAt"),
                employment_type=j.get("employmentType"),
                department=j.get("department"),
                raw=_jdump(j),
                fmt="json",
                text=text.strip(),
            )
        )
    return Listing(ref, tuple(out))


# --- Workable ---------------------------------------------------------------------------------


class Workable:
    system = "workable"

    def guessable(self, slug: str) -> bool:
        return bool(_SLUG.match(slug))

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{quote(ref.slug)}?details=true"
        body = read_feed(fetcher.get(url, accept="application/json"))
        return None if body is None else parse_workable(ref, body)


def parse_workable(ref: BoardRef, body: str) -> Listing:
    data = json.loads(body)
    out = []
    for j in data.get("jobs") or []:
        location = ", ".join(x for x in (_plain(j.get("city")), _plain(j.get("country"))) if x)
        out.append(
            _feed_job(
                id=str(j.get("shortcode") or j.get("url")),
                title=_plain(j.get("title")),
                url=j.get("url") or j.get("shortlink") or "",
                location=location,
                posted_at=j.get("published_on") or j.get("created_at"),
                employment_type=j.get("employment_type"),
                department=j.get("department"),
                raw=_jdump(j),
                fmt="json",
                text=extract_text(j.get("description") or ""),
            )
        )
    return Listing(ref, tuple(out), _plain(data.get("name")))


# --- Recruitee --------------------------------------------------------------------------------


class Recruitee:
    system = "recruitee"

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug))

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        body = read_feed(fetcher.get(f"{_recruitee_site(ref)}/api/offers", accept="application/json"))
        return None if body is None else parse_recruitee(ref, body)

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        url = f"{_recruitee_site(ref)}/api/offers/{quote(job_id)}"
        body = read_feed(fetcher.get(url, accept="application/json"))
        if body is None:
            return None
        offer = json.loads(body).get("offer")
        jobs = parse_recruitee(ref, json.dumps({"offers": [offer]})).jobs if offer else ()
        return jobs[0] if jobs else None


def _recruitee_site(ref: BoardRef) -> str:
    """`<slug>.recruitee.com`, or the company's own host when the board is served there."""
    return f"https://{ref.slug}" if "." in ref.slug else f"https://{ref.slug}.recruitee.com"


def parse_recruitee(ref: BoardRef, body: str) -> Listing:
    offers = json.loads(body).get("offers") or []
    out = []
    name = ""
    for j in offers:
        if j.get("status") not in (None, "published"):
            continue
        name = name or _plain(j.get("company_name"))
        j = {k: v for k, v in j.items() if k != "mailbox_email"}
        out.append(
            _feed_job(
                id=str(j["id"]),
                title=_plain(j.get("title")),
                url=j.get("careers_url") or "",
                location=j.get("location"),
                posted_at=j.get("published_at"),
                employment_type=j.get("employment_type_code"),
                department=j.get("department"),
                salary=_salary(
                    (j.get("salary") or {}).get("min"),
                    (j.get("salary") or {}).get("max"),
                    (j.get("salary") or {}).get("currency"),
                    (j.get("salary") or {}).get("period"),
                ),
                raw=_jdump(j),
                fmt="json",
                text=extract_text((j.get("description") or "") + (j.get("requirements") or "")),
            )
        )
    return Listing(ref, tuple(out), name)


# --- Teamtailor -------------------------------------------------------------------------------


class Teamtailor:
    """Career sites on `<slug>.teamtailor.com` or on the company's own host; both serve /jobs.rss."""

    system = "teamtailor"

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug))

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        host = ref.slug if "." in ref.slug else f"{ref.slug}.teamtailor.com"
        body = read_feed(fetcher.get(f"https://{host}/jobs.rss", accept="application/rss+xml"))
        if body is None or "<rss" not in body[:1000]:
            return None
        return parse_teamtailor(ref, body)


def _tt_text(item: Any, name: str) -> str:
    for el in item.iter():
        if isinstance(el.tag, str) and el.tag.endswith("}" + name) and el.text:
            return _plain(el.text)
    return ""


def parse_teamtailor(ref: BoardRef, body: str) -> Listing:
    root = etree.fromstring(body.encode("utf-8"), parser=etree.XMLParser(recover=True))
    channel = root.find("channel")
    if channel is None:
        raise BoardError(f"teamtailor {ref.slug}: no RSS channel")
    out = []
    for item in channel.findall("item"):
        link = (item.findtext("link") or "").strip()
        locations = [
            _plain(el.text)
            for el in item.iter()
            if isinstance(el.tag, str) and el.tag.endswith("}city") and el.text
        ]
        out.append(
            _feed_job(
                id=(item.findtext("guid") or link).strip(),
                title=_plain(item.findtext("title")),
                url=link,
                location=", ".join(dict.fromkeys(locations)),
                posted_at=item.findtext("pubDate"),
                department=_tt_text(item, "department"),
                raw=etree.tostring(item, encoding="unicode"),
                fmt="xml",
                text=extract_text(item.findtext("description") or ""),
            )
        )
    return Listing(ref, tuple(out), _plain(channel.findtext("title")))


# --- HR Manager (Talentech) -------------------------------------------------------------------


_HRM_PERSONAL = ("ProjectLeader", "ProjectParticipants", "Users", "ProjectAdministratorId")


class HrManager:
    system = "hrmanager"
    page_size = 100

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9]{2,40}$", slug))

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        pages = []
        skip = 0
        while True:
            url = (
                f"https://api.hr-manager.net/jobportal.svc/{quote(ref.slug)}/positionlist/json/"
                f"?incAds=1&take={self.page_size}&skip={skip}"
            )
            response = fetcher.get(url, accept="application/json")
            if response.status == 400 and '"CustomerAlias":null' in response.text:
                return None  # HR Manager's answer to an unknown alias
            body = read_feed(response)
            if body is None:
                return None
            pages.append(body)
            data = json.loads(body)
            skip += self.page_size
            if not data.get("Items") or skip >= int(data.get("PositionCountCustomer") or 0):
                break
        return parse_hrmanager(ref, pages)


def parse_hrmanager(ref: BoardRef, pages: list[str]) -> Listing:
    out = []
    name = ""
    for body in pages:
        data = json.loads(body)
        name = name or _plain(data.get("CustomerName"))
        for item in data.get("Items") or []:
            # Data minimization: the feed names the hiring manager and project members. Those
            # are people, not the ad, so they are dropped before anything is stored.
            item = {k: v for k, v in item.items() if not k.startswith(_HRM_PERSONAL)}
            ads = item.get("Advertisements") or []
            content = "".join(a.get("Content") or "" for a in ads)
            location = (item.get("PositionLocation") or {}).get("Name") or item.get("WorkPlace")
            out.append(
                _feed_job(
                    id=str(item["Id"]),
                    title=_plain(item.get("Name")),
                    url=item.get("AdvertisementUrlSecure") or item.get("AdvertisementUrl") or "",
                    location=location,
                    posted_at=item.get("Published") or item.get("Created"),
                    employment_type=item.get("WorkHours") or item.get("PositionType"),
                    department=item.get("DepartmentNamePlainText")
                    or (item.get("Department") or {}).get("Name"),
                    raw=_jdump(item),
                    fmt="json",
                    text=extract_text(content) or _plain(item.get("ShortDescription")),
                )
            )
    return Listing(ref, tuple(out), name)


# --- Emply ------------------------------------------------------------------------------------

_EMPLY_SECTION = re.compile(r"sectionId\s*:\s*['\"]([0-9a-f-]{36})['\"]")
_EMPLY_LANG = re.compile(r"languageKey\s*=\s*['\"]([a-z]{2})['\"]")


class Emply:
    """Emply career sites run a jobs widget that posts to /api/integration/vacancy/get-page.

    `slug` is the career-site host (or the `<slug>` of `<slug>.career.emply.com`); `extra` is
    the widget's section id, read from the jobs page when not already known.
    """

    system = "emply"
    page_size = 100

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug))

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        host = ref.slug if "." in ref.slug else f"{ref.slug}.career.emply.com"
        section, lang = ref.extra, "da"
        if not section:
            found = self._find_section(fetcher, host)
            if found is None:
                return None
            section, lang = found
        vacancies: list[dict[str, Any]] = []
        while True:
            response = fetcher.post_json(
                f"https://{host}/api/integration/vacancy/get-page",
                {
                    "count": self.page_size,
                    "filters": [],
                    "langCode": lang,
                    "offset": len(vacancies),
                    "searchText": "",
                    "sectionId": section,
                    "sortByProjectDataId": "",
                    "sortAscending": False,
                    "light": False,
                    "isJobAgent": False,
                    "siteId": None,
                },
            )
            body = read_feed(response)
            if body is None:
                return None
            data = json.loads(body)
            batch = data.get("vacancies") or []
            vacancies.extend(batch)
            if not batch or len(vacancies) >= int(data.get("count") or 0):
                break
        return parse_emply(BoardRef(self.system, ref.slug, section), host, lang, vacancies)

    def _find_section(self, fetcher: Fetcher, host: str) -> tuple[str, str] | None:
        for path in ("/ledige-stillinger", "/da/ledige-stillinger", "/en/vacancies", "/"):
            response = fetcher.get(f"https://{host}{path}", accept="text/html")
            if host not in response.final_url:
                return None  # unknown Emply sites redirect to emply.com
            if not response.ok:
                continue
            found = find_emply_section(response.text)
            if found:
                return found
        return None


def find_emply_section(html: str) -> tuple[str, str] | None:
    """The jobs widget's section id and language on an Emply page, if it has one."""
    # The widget's config (with its section id) is declared well before the jobs call that
    # uses it, often 15k+ characters earlier; take the last one declared before the call.
    call = html.find("vacancy/get-page")
    if call < 0:
        return None
    sections = list(_EMPLY_SECTION.finditer(html, 0, call))
    if not sections:
        return None
    match = sections[-1]
    lang = _EMPLY_LANG.search(html)
    return match.group(1), lang.group(1) if lang else "da"


def parse_emply(ref: BoardRef, host: str, lang: str, vacancies: list[dict[str, Any]]) -> Listing:
    out = []
    for v in vacancies:
        translations = v.get("translations") or []
        content = translations[0].get("content", "") if translations else ""
        url = f"https://{host}/{lang}/ad/{v.get('titleAsUrl', '')}/{v.get('shortId', '')}"
        out.append(
            _feed_job(
                id=str(v["id"]),
                title=_plain(v.get("title")),
                url=v.get("directLink") or url,
                location=v.get("location"),
                posted_at=v.get("published") or v.get("created"),
                department=v.get("department"),
                raw=_jdump(v),
                fmt="json",
                text=extract_text(content),
            )
        )
    return Listing(ref, tuple(out))


# --- JOIN -------------------------------------------------------------------------------------

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_JOIN_PERSONAL = ("contactName", "contactTitle", "contactPictureId", "contactEmail", "contactPhone")


class Join:
    """join.com has a token-only API, but its public company page embeds the job list
    (paginated with `?page=N`) and each job page embeds the full ad, both as Next.js data."""

    system = "join"

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug))

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        base = f"https://join.com/companies/{quote(ref.slug)}"
        items: list[dict[str, Any]] = []
        company: dict[str, Any] = {}
        page = 1
        while True:
            body = read_feed(fetcher.get(f"{base}?page={page}" if page > 1 else base, accept="text/html"))
            if body is None:
                return None
            state = _next_state(body)
            if state is None:
                return None
            company = company or state.get("company") or {}
            jobs = state.get("jobs") or {}
            items.extend(jobs.get("items") or [])
            pagination = jobs.get("pagination") or {}
            if page >= int(pagination.get("pageCount") or 1):
                break
            page += 1
        if not company:
            return None
        slug = company.get("domain") or ref.slug
        details = []
        for item in items:
            url = f"https://join.com/companies/{slug}/{item.get('idParam') or item['id']}"
            response = fetcher.get(url, accept="text/html")
            state = _next_state(response.text) if response.ok else None
            details.append((url, item, (state or {}).get("job") or {}))
        return parse_join(BoardRef(self.system, slug), company, details)

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        # join.com redirects /companies/<slug>/<id> to the job's full /<id>-<title> URL.
        response = fetcher.get(
            f"https://join.com/companies/{quote(ref.slug)}/{quote(job_id)}", accept="text/html"
        )
        if response.status in (404, 410):
            return None
        body = read_feed(response)
        state = _next_state(body or "")
        job = (state or {}).get("job")
        if not job or str(job.get("id")) != job_id or job.get("status") not in (None, "ONLINE"):
            return None
        item = {"id": job["id"], "title": job.get("title"), "city": job.get("city")}
        jobs = parse_join(ref, {}, [(response.final_url, item, job)]).jobs
        return jobs[0] if jobs else None


def _join_salary(job: dict[str, Any]) -> Salary | None:
    """JOIN states amounts in cents. A company can hide its salary (`settings.showSalary`
    false); then it is not reported, even though the page data carries it."""
    if not (job.get("settings") or {}).get("showSalary"):
        return None
    low, high = job.get("salaryAmountFrom") or {}, job.get("salaryAmountTo") or {}
    period = {"PER_YEAR": "year", "PER_MONTH": "month", "PER_HOUR": "hour"}.get(
        str(job.get("salaryFrequency")), job.get("salaryFrequency")
    )
    return _salary(
        low.get("amount"), high.get("amount"), low.get("currency") or high.get("currency"), period, 100
    )


def _next_state(html: str) -> dict[str, Any] | None:
    match = _NEXT_DATA.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))["props"]["pageProps"]["initialState"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return None


def parse_join(
    ref: BoardRef, company: dict[str, Any], details: list[tuple[str, dict[str, Any], dict[str, Any]]]
) -> Listing:
    out = []
    for url, item, job in details:
        job = {k: v for k, v in job.items() if k not in _JOIN_PERSONAL and k != "company"}
        sections = [job.get(k) or "" for k in ("intro", "tasks", "requirements", "benefits")]
        text = "\n\n".join(x.strip() for x in sections if x.strip())
        if not text:
            text = extract_text(job.get("schemaDescription") or "")
        city = job.get("city") or item.get("city") or {}
        location = ", ".join(x for x in (_plain(city.get("cityName")), _plain(city.get("countryName"))) if x)
        out.append(
            _feed_job(
                id=str(item["id"]),
                title=_plain(item.get("title") or job.get("title")),
                url=url,
                location=location,
                posted_at=job.get("createdAt") or item.get("createdAt"),
                employment_type=(job.get("employmentType") or item.get("employmentType") or {}).get("name"),
                salary=_join_salary(job or item),
                raw=_jdump(job or item),
                fmt="json",
                text=text or None,
            )
        )
    return Listing(ref, tuple(out), _plain(company.get("name")), company.get("url") or "")


# --- HiBob -------------------------------------------------------------------------------------


class HiBob:
    """HiBob career sites (`<slug>.careers.hibob.com`) load their ads from /api/job-ad, which
    answers JSON to a request that comes from the career site (Referer set), else 401."""

    system = "hibob"

    def guessable(self, slug: str) -> bool:
        return bool(re.match(r"^[a-z0-9][a-z0-9-]{1,62}$", slug))

    def fetch(self, fetcher: Fetcher, ref: BoardRef) -> Listing | None:
        site = f"https://{ref.slug}.careers.hibob.com"
        response = fetcher.get(f"{site}/api/job-ad", accept="application/json", referer=f"{site}/jobs")
        body = read_feed(response, missing=(404, 401, 403))
        if body is None or not body.lstrip().startswith("{"):
            return None
        return parse_hibob(ref, body)

    def fetch_job(self, fetcher: Fetcher, ref: BoardRef, job_id: str) -> Job | None:
        return job_from_listing(self, fetcher, ref, job_id)  # no single-job endpoint


def parse_hibob(ref: BoardRef, body: str) -> Listing:
    out = []
    for j in json.loads(body).get("jobAdDetails") or []:
        parts = [j.get(k) or "" for k in ("description", "responsibilities", "requirements", "benefits")]
        location = ", ".join(x for x in (_plain(j.get("site")), _plain(j.get("country"))) if x)
        out.append(
            _feed_job(
                id=str(j["id"]),
                title=_plain(j.get("title")),
                url=f"https://{ref.slug}.careers.hibob.com/jobs/{j['id']}",
                location=location,
                posted_at=j.get("publishedAt"),
                employment_type=j.get("employmentType"),
                department=j.get("department"),
                salary=_salary(
                    j.get("payTransparencyMinSalary"),
                    j.get("payTransparencyMaxSalary"),
                    j.get("payTransparencySalaryCurrency"),
                    j.get("payTransparencySalaryPayPeriod"),
                ),
                raw=_jdump(j),
                fmt="json",
                text=extract_text("\n".join(f"<div>{p}</div>" for p in parts if p)),
            )
        )
    return Listing(ref, tuple(out))


def public_url(ref: BoardRef) -> str:
    """The board page a person would open; empty when the system has no public listing URL."""
    s, eu = ref.slug, ref.extra == "eu"
    if ref.system == "greenhouse":
        return f"https://job-boards{'.eu' if eu else ''}.greenhouse.io/{s}"
    if ref.system == "lever":
        return f"https://jobs.{'eu.' if eu else ''}lever.co/{s}"
    if ref.system == "ashby":
        return f"https://jobs.ashbyhq.com/{s}"
    if ref.system == "workable":
        return f"https://apply.workable.com/{s}/"
    if ref.system == "recruitee":
        return f"https://{s if '.' in s else s + '.recruitee.com'}/"
    if ref.system == "teamtailor":
        return f"https://{s if '.' in s else s + '.teamtailor.com'}/jobs"
    if ref.system == "emply":
        return f"https://{s if '.' in s else s + '.career.emply.com'}/ledige-stillinger"
    if ref.system == "join":
        return f"https://join.com/companies/{s}"
    if ref.system == "hibob":
        return f"https://{s}.careers.hibob.com/jobs"
    return ""


ADAPTERS: dict[str, Adapter] = {
    a.system: a
    for a in (
        Greenhouse(),
        Lever(),
        Ashby(),
        Workable(),
        Recruitee(),
        Teamtailor(),
        HrManager(),
        Emply(),
        Join(),
        HiBob(),
    )
}
