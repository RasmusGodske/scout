"""Completing a certificate chain a server sent without its intermediate. No real network."""

from __future__ import annotations

import ssl
from pathlib import Path

import httpx

from scout.modules.fetch import Fetcher, FetchPolicy
from scout.modules.fetch._tls import (
    ChainCompleter,
    ca_issuers_urls,
    certificate_der,
    missing_issuer,
    ssl_context,
)

SUPPORT = Path(__file__).parent / "support"
# The certificate job.reqruiting.com serves, alone, without Let's Encrypt's YR1 intermediate.
LEAF = (SUPPORT / "reqruiting_leaf.der").read_bytes()
_UNVERIFIED = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
    "unable to get local issuer certificate (_ssl.c:1010)"
)


def test_reads_the_issuer_url_from_a_real_certificate() -> None:
    assert ca_issuers_urls(LEAF) == ["http://yr1.i.lencr.org/"]
    assert ca_issuers_urls(b"\x30\x03\x02\x01\x00") == []


def test_accepts_der_and_pem_issuer_certificates_only() -> None:
    assert certificate_der(LEAF) == LEAF
    assert certificate_der(ssl.DER_cert_to_PEM_cert(LEAF).encode()) == LEAF
    assert certificate_der(b"<html>not found</html>") is None


def test_recognises_a_missing_intermediate_and_nothing_else() -> None:
    err = ssl.SSLCertVerificationError(1, _UNVERIFIED)
    err.verify_code = 20
    assert missing_issuer(httpx.ConnectError(_UNVERIFIED))
    wrapped = httpx.ConnectError("handshake failed")
    wrapped.__cause__ = err
    assert missing_issuer(wrapped)
    expired = ssl.SSLCertVerificationError(1, "certificate has expired")
    expired.verify_code = 10
    assert not missing_issuer(expired)
    assert not missing_issuer(httpx.ConnectError("[Errno 111] Connection refused"))


def test_completer_tries_each_host_once_and_never_allows_partial_chains() -> None:
    context = ssl_context()
    assert not context.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    downloads: list[str] = []
    completer = ChainCompleter(context, lambda url: downloads.append(url) or None, leaf=lambda h, p: LEAF)
    # The download gives nothing usable, so nothing is added and the host is not tried again.
    assert not completer.complete("https://job.reqruiting.com/job.aspx?id=1")
    assert not completer.complete("https://job.reqruiting.com/job.aspx?id=2")
    assert downloads == ["http://yr1.i.lencr.org/"]


def test_fetcher_retries_once_after_completing_the_chain() -> None:
    calls = {"n": 0}

    def server(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # the robots.txt request meets the incomplete chain first
            raise httpx.ConnectError(_UNVERIFIED, request=request)
        return httpx.Response(200, text="<p>job</p>")

    f = Fetcher(
        FetchPolicy(user_agent="t", min_interval_seconds=0, retries=0), transport=httpx.MockTransport(server)
    )
    completed: list[str] = []

    class Completer:
        def complete(self, url: str) -> bool:
            completed.append(url)
            return True

    f._chains = Completer()  # type: ignore[assignment]
    assert f.get("https://job.reqruiting.com/job.aspx?id=1").ok
    assert completed == ["https://job.reqruiting.com/robots.txt"]
