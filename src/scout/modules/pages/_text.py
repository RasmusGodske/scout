from __future__ import annotations

import logging

import trafilatura
from lxml import html as lxml_html

# trafilatura logs an ERROR for every fragment it cannot parse; we fall back quietly instead.
logging.getLogger("trafilatura").setLevel(logging.CRITICAL)


def extract_text(html: str) -> str:
    """The readable text of a job page or an HTML fragment from a feed.

    trafilatura finds the main content of a full page; a short fragment it may reject, so
    plain text-content with block breaks is the fallback.
    """
    if not html or not html.strip():
        return ""
    text = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_recall=True, deduplicate=True
    )
    if text and len(text) >= 200:
        return text.strip()
    fallback = _plain_text(html)
    return fallback if len(fallback) > len(text or "") else (text or "").strip()


def page_title(html: str) -> str:
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for xp in ("//h1", "//title", "//meta[@property='og:title']/@content"):
        found = doc.xpath(xp)
        if found:
            value = found[0] if isinstance(found[0], str) else found[0].text_content()
            value = " ".join(value.split())
            if value:
                return value[:300]
    return ""


_BLOCKS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}


def _plain_text(html: str) -> str:
    try:
        doc = lxml_html.fromstring(html)
    except (ValueError, lxml_html.etree.ParserError):
        return ""
    for bad in doc.xpath("//script|//style|//noscript|//template|//nav|//footer|//header"):
        bad.drop_tree()
    for el in doc.iter():
        if isinstance(el.tag, str) and el.tag in _BLOCKS:
            el.tail = "\n" + (el.tail or "")
    lines = (" ".join(line.split()) for line in doc.text_content().splitlines())
    return "\n".join(line for line in lines if line).strip()
