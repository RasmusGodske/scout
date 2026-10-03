"""Job details stated plainly by a page: schema.org JobPosting markup (JSON-LD), then the page
heading, then the link text. Pure; nothing here reads or interprets the ad's prose."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from lxml import html as lxml_html

from scout.modules.pages._links import canonical_url
from scout.modules.pages._text import page_title
from scout.modules.results import DetailSource, JobDetails, Salary

_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def normalize_date(value: Any) -> str | None:
    """A date as the site states it, in ISO 8601: `YYYY-MM-DD` for a bare date, else a UTC
    timestamp `YYYY-MM-DDTHH:MM:SSZ`. Understands ISO strings, epoch milliseconds, .NET
    `/Date(ms+0200)/` and RFC 822 (RSS). Anything else is None, never a guess."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return _utc(datetime.fromtimestamp(seconds, UTC))
    text = str(value).strip()
    match = _DOTNET_DATE.match(text)
    if match:
        return _utc(datetime.fromtimestamp(int(match.group(1)) / 1000, UTC))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    iso = text.replace(" UTC", "+00:00").replace("Z", "+00:00")
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)  # Python takes at most microseconds
    if re.match(r"^\d{4}-\d{2}-\d{2} \d", iso):
        iso = iso.replace(" ", "T", 1)
    try:
        return _utc(datetime.fromisoformat(iso))
    except ValueError:
        pass
    try:
        return _utc(parsedate_to_datetime(text))
    except (TypeError, ValueError, IndexError):
        return None


def details_from_page(
    html: str, page_url: str, link_text: str = ""
) -> tuple[JobDetails, dict[str, DetailSource]]:
    """Details for a job on a custom careers page, each field from the first place that states
    it: the page's JSON-LD JobPosting, then (title only) its heading, then the link text."""
    fields: dict[str, Any] = {}
    sources: dict[str, DetailSource] = {}
    posting = find_job_posting(html, page_url)
    if posting is not None:
        for name, value in job_posting_fields(posting).items():
            if value not in (None, ""):
                fields[name] = value
                sources[name] = DetailSource.JSON_LD
    if "title" not in fields:
        heading = page_title(html)
        if heading:
            fields["title"], sources["title"] = heading, DetailSource.PAGE_HEADING
        elif link_text.strip():
            fields["title"], sources["title"] = " ".join(link_text.split()), DetailSource.LINK_TEXT
    return JobDetails(**fields), sources


def find_job_posting(html: str, page_url: str = "") -> dict[str, Any] | None:
    """The page's schema.org JobPosting, if it has one. A page listing several (e.g. "similar
    jobs") gives the one whose url is this page, else the first."""
    postings = [p for block in _json_ld_blocks(html) for p in _walk_postings(block)]
    if not postings:
        return None
    if page_url:
        here = canonical_url(page_url)
        for posting in postings:
            url = posting.get("url")
            if isinstance(url, str) and url and canonical_url(url) == here:
                return posting
    return postings[0]


def job_posting_fields(posting: dict[str, Any]) -> dict[str, Any]:
    """schema.org JobPosting -> JobDetails fields. The standard allows a few shapes per field
    (one place or a list, a string or a list, nested objects); all are handled here, once."""
    return {
        "title": _clean(posting.get("title")),
        "location": _location(posting),
        "posted_at": normalize_date(posting.get("datePosted")),
        "employment_type": _join(posting.get("employmentType")),
        "department": None,  # schema.org has no department field
        "salary": _salary(posting.get("baseSalary")),
    }


# --- JSON-LD ----------------------------------------------------------------------------------


def _json_ld_blocks(html: str) -> list[Any]:
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return []
    blocks = []
    for script in doc.xpath("//script"):
        if (script.get("type") or "").strip().lower() != "application/ld+json":
            continue
        text = (script.text or "").strip()
        if not text:
            continue
        for candidate in (text, html_lib.unescape(text)):
            try:
                blocks.append(json.loads(candidate, strict=False))
                break
            except json.JSONDecodeError:
                continue
    return blocks


def _walk_postings(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [p for item in node for p in _walk_postings(item)]
    if not isinstance(node, dict):
        return []
    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if "JobPosting" in types:
        return [node]
    return _walk_postings(node.get("@graph", []))


def _location(posting: dict[str, Any]) -> str | None:
    places = posting.get("jobLocation")
    places = places if isinstance(places, list) else [places] if places else []
    names = []
    for place in places:
        if isinstance(place, str):
            names.append(_clean(place))
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address")
        if isinstance(address, str):
            names.append(_clean(address))
        elif isinstance(address, dict):
            country = address.get("addressCountry")
            if isinstance(country, dict):
                country = country.get("name")
            parts = [_clean(address.get("addressLocality")), _clean(country)]
            names.append(", ".join(p for p in parts if p))
        elif place.get("name"):
            names.append(_clean(place.get("name")))
    if str(posting.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        names.append("Remote")
    names = [n for n in dict.fromkeys(names) if n]
    return "; ".join(names) or None


def _salary(value: Any) -> Salary | None:
    if not isinstance(value, dict):
        return None
    amount = value.get("value")
    low = high = None
    period = None
    if isinstance(amount, dict):
        low = _number(amount.get("minValue"))
        high = _number(amount.get("maxValue"))
        single = _number(amount.get("value"))
        if low is None and high is None and single is not None:
            low = high = single
        period = amount.get("unitText")
    else:
        low = high = _number(amount)
    if low is None and high is None:
        return None
    return Salary(low, high, _clean(value.get("currency")), _clean(period).lower() if period else None)


# --- small values -----------------------------------------------------------------------------


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, dict | list):
        return None
    text = " ".join(html_lib.unescape(str(value)).split())
    return text or None


def _join(value: Any) -> str | None:
    items = value if isinstance(value, list) else [value]
    cleaned = [c for c in (_clean(v) for v in items) if c]
    return ", ".join(dict.fromkeys(cleaned)) or None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
