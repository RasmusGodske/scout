"""Reading custom careers pages: links, careers-word scoring, job-link filtering, text.

Every function here is pure: HTML and URLs in, data out. See README.md.
"""

from scout.modules.pages._details import (
    details_from_page,
    find_job_posting,
    job_posting_fields,
    normalize_date,
)
from scout.modules.pages._links import (
    Link,
    canonical_url,
    careers_candidates,
    careers_score,
    collect_links,
    job_link_candidates,
    job_link_heuristic,
    same_site,
)
from scout.modules.pages._text import extract_text, page_title

__all__ = [
    "Link",
    "canonical_url",
    "careers_candidates",
    "careers_score",
    "collect_links",
    "details_from_page",
    "extract_text",
    "find_job_posting",
    "job_link_candidates",
    "job_link_heuristic",
    "job_posting_fields",
    "normalize_date",
    "page_title",
    "same_site",
]
