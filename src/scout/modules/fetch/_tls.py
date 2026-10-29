"""Completing a server's certificate chain the way browsers do (AIA fetching).

Some servers send only their own certificate and leave out the intermediate that links it to a
trusted root. Browsers fetch the missing intermediate from the URL the certificate names
(Authority Information Access, "CA Issuers"); Python's ssl does not, so such a site reads as
unreachable to scout while it works for every visitor. The hiring system Reqruiting
(`*.reqruiting.com`) is one: its job pages were unreadable for every company using it.

The fetched intermediate is added to the verification store only as a chain-building aid: the
chain must still end at a root in certifi's bundle (partial chains are refused), so a forged
intermediate cannot make an untrusted certificate pass.
"""

from __future__ import annotations

import logging
import ssl
import threading
from collections.abc import Callable
from urllib.parse import urlsplit

import certifi

log = logging.getLogger(__name__)

# The DER encoding of OID 1.3.6.1.5.5.7.48.2 (id-ad-caIssuers).
_CA_ISSUERS_OID = bytes.fromhex("06082b06010505073002")
# The context tag of a GeneralName's uniformResourceIdentifier ([6] IA5String).
_URI_TAG = 0x86
# OpenSSL's X509_V_ERR_UNABLE_TO_GET_ISSUER_CERT_LOCALLY: the server left out an intermediate.
_MISSING_ISSUER = 20
# How many issuers up the chain to fetch at most.
_MAX_DEPTH = 3


def ssl_context() -> ssl.SSLContext:
    """certifi's roots, as httpx uses by default, and never a partial chain."""
    context = ssl.create_default_context(cafile=certifi.where())
    context.verify_flags &= ~getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    return context


def missing_issuer(exc: BaseException) -> bool:
    """Whether a connection failed because the server's chain lacks an intermediate."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError):
            return seen.verify_code == _MISSING_ISSUER
        if "unable to get local issuer certificate" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def ca_issuers_urls(der: bytes) -> list[str]:
    """The http(s) URLs a DER certificate names for its issuer's certificate. Pure."""
    urls = []
    start = 0
    while (i := der.find(_CA_ISSUERS_OID, start)) >= 0:
        start = i + len(_CA_ISSUERS_OID)
        if start + 2 > len(der) or der[start] != _URI_TAG:
            continue
        length, offset = der[start + 1], start + 2
        if length & 0x80:  # long form: the next (length & 0x7f) bytes hold the length
            n = length & 0x7F
            length, offset = int.from_bytes(der[offset : offset + n], "big"), offset + n
        url = der[offset : offset + length].decode("ascii", errors="replace")
        if urlsplit(url).scheme in ("http", "https"):
            urls.append(url)
    return urls


def certificate_der(body: bytes) -> bytes | None:
    """An issuer certificate as served: DER (`application/pkix-cert`) or PEM. Anything else,
    such as a PKCS#7 bundle, is not used."""
    if body.startswith(b"-----BEGIN CERTIFICATE-----"):
        try:
            return ssl.PEM_cert_to_DER_cert(body.decode("ascii"))
        except ValueError:
            return None
    # A DER certificate is a SEQUENCE whose first element is the tbsCertificate SEQUENCE.
    if len(body) > 4 and body[0] == 0x30 and 0x30 in body[2:6]:
        return body
    return None


class ChainCompleter:
    """Adds the missing intermediate for a host to `context`, at most once per host.

    `leaf` returns the server's own certificate (DER) without verifying it, and `download`
    fetches a URL's body; both are parameters so the logic is tested without a network.
    """

    def __init__(
        self,
        context: ssl.SSLContext,
        download: Callable[[str], bytes | None],
        leaf: Callable[[str, int], bytes] | None = None,
    ) -> None:
        self._context = context
        self._download = download
        self._leaf = leaf or _leaf_certificate
        self._tried: set[str] = set()
        self._lock = threading.Lock()

    def complete(self, url: str) -> bool:
        """True when an intermediate was added and the request is worth sending again."""
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port or 443
        with self._lock:
            if not host or host in self._tried:
                return False
            self._tried.add(host)
        try:
            cert = self._leaf(host, port)
        except OSError as exc:
            log.info("could not read %s's certificate: %s", host, exc)
            return False
        # Walk up: an intermediate may itself be issued by a newer root that is cross-signed by
        # an older one (Let's Encrypt's YR1 -> Root YR -> ISRG Root X1).
        added = []
        for _ in range(_MAX_DEPTH):
            issuer = self._issuer(cert)
            if issuer is None:
                break
            url, cert = issuer
            added.append(url)
        if added:
            log.info("completed %s's certificate chain with %s", host, ", ".join(added))
        return bool(added)

    def _issuer(self, cert: bytes) -> tuple[str, bytes] | None:
        for url in ca_issuers_urls(cert)[:2]:
            body = self._download(url)
            der = certificate_der(body) if body else None
            if der is None or der == cert:
                continue
            try:
                self._context.load_verify_locations(cadata=der)
            except ssl.SSLError as exc:
                log.info("issuer certificate from %s not usable: %s", url, exc)
                continue
            return url, der
        return None


def _leaf_certificate(host: str, port: int) -> bytes:
    # get_server_certificate does not verify (ca_certs=None): only the AIA URL is read from it.
    return ssl.PEM_cert_to_DER_cert(ssl.get_server_certificate((host, port), timeout=10))
