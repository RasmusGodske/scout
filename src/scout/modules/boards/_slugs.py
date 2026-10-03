"""Slugs a company might use on a hiring system, derived from its domain. Pure."""

from __future__ import annotations

import re

_LEGAL_FORMS = re.compile(
    r"\b(aps|a/s|as|a\.s\.|i/s|p/s|k/s|ivs|ltd|limited|inc|gmbh|ab|oy|bv|nv|sa|group|holding|"
    r"danmark|denmark|nordic)\b",
    re.I,
)


def domain_stem(domain: str) -> tuple[str, str]:
    """(stem, tld) of a bare domain: `www.acme-group.co.uk` -> (`acme-group`, `uk`)."""
    host = domain.lower().strip().removeprefix("http://").removeprefix("https://").split("/")[0]
    host = host.removeprefix("www.")
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in ("co", "com", "org", "net", "ac"):
        return labels[-3], labels[-1]
    if len(labels) >= 2:
        return labels[-2], labels[-1]
    return host, ""


def slug_candidates(domain: str, suffixes: list[str], company_name: str = "") -> list[str]:
    """`acme.dk` -> acme, acme-dk, acmeaps, ...; a company name adds its own spelling."""
    stem, tld = domain_stem(domain)
    out = [stem, stem.replace("-", "")]
    if tld:
        out.append(f"{stem}-{tld}")
    out += [f"{stem.replace('-', '')}{s}" for s in suffixes]
    if company_name:
        name = _LEGAL_FORMS.sub(" ", company_name.lower())
        words = re.findall(r"[a-z0-9æøå]+", name)
        if words:
            out += ["".join(words), "-".join(words)]
    return [s for s in dict.fromkeys(_ascii(s) for s in out) if len(s) >= 2]


def name_matches(domain: str, company_name: str) -> bool:
    """Whether a board's company name plausibly belongs to the domain's company."""
    stem = _norm(domain_stem(domain)[0])
    name = _norm(_LEGAL_FORMS.sub(" ", company_name))
    if not stem or not name:
        return False
    short, long = sorted((stem, name), key=len)
    return len(short) >= 3 and short in long


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _ascii(value.lower()))


def _ascii(value: str) -> str:
    return value.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
