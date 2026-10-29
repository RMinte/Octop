"""Outbound HTTPS URL validation — mitigates SSRF (CWE-918)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import typing
from urllib.parse import urlparse, urlunparse

import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION, AsyncNetworkStream


class UnsafeOutboundUrl(ValueError):
    """Raised when a URL must not be fetched server-side."""


# Hostnames that never refer to a public Internet endpoint (Docker Desktop, etc.).
_LOCAL_HOSTNAMES = frozenset(
    {
        "localhost",
        "host.docker.internal",
        "gateway.docker.internal",
    }
)
_LOCAL_HOST_SUFFIXES = (".local", ".localhost", ".internal")

# Shadowrocket and other TUN proxies synthesize addresses in the benchmark
# range. These are routing tokens, never trusted outbound destinations.
_FAKE_IP_NETWORK = ipaddress.IPv4Network("198.18.0.0/15")
_IPV4_TRANSLATED_NETWORK = ipaddress.IPv6Network("::ffff:0:0:0/96")


def _is_proxy_fake_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        elif addr in _IPV4_TRANSLATED_NETWORK:
            addr = ipaddress.IPv4Address(addr.packed[-4:])
    return isinstance(addr, ipaddress.IPv4Address) and addr in _FAKE_IP_NETWORK


def is_private_or_local_host(host: str) -> bool:
    """True for loopback / RFC1918 / link-local literals and well-known local names.

    Used by MCP connector URL validation so self-hosted LAN endpoints are allowed
    without weakening the outbound SSRF guard used for OAuth / provider probes.
    """
    normalized = host.lower().rstrip(".")
    if not normalized:
        return False
    if normalized in _LOCAL_HOSTNAMES:
        return True
    if normalized.endswith(_LOCAL_HOST_SUFFIXES):
        return True
    try:
        addr = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private or addr.is_link_local


def _parse_https_host(url: str) -> tuple[str, int | None]:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise UnsafeOutboundUrl("invalid URL") from exc
    if parsed.scheme != "https":
        raise UnsafeOutboundUrl("only https URLs are allowed")
    # Match httpcore's ASCII host so IDNA normalization cannot bypass IP pinning.
    host = parsed.raw_host.decode("ascii").rstrip(".")
    if not host:
        raise UnsafeOutboundUrl("missing hostname")
    return host, parsed.port


def _check_ip_not_private(ip_str: str) -> None:
    addr = ipaddress.ip_address(ip_str)
    if (
        not addr.is_global
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    ):
        raise UnsafeOutboundUrl("private or reserved IP addresses are not allowed")


def _check_ip_literal(host: str) -> None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return
    _check_ip_not_private(host)


def _check_resolved_ip(ip_str: str) -> None:
    _check_ip_not_private(ip_str)


def issuer_base_domain(issuer: str) -> str:
    host = (urlparse(issuer).hostname or "").lower()
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def host_allowed_for_issuer(host: str, issuer: str) -> bool:
    normalized = host.lower().rstrip(".")
    issuer_host = (urlparse(issuer).hostname or "").lower()
    base = issuer_base_domain(issuer)
    if normalized in {issuer_host, base}:
        return True
    return normalized.endswith(f".{base}")


def validate_https_url(url: str, *, field: str = "url") -> str:
    """Reject non-https URLs and literal private/reserved IPs."""
    host, _ = _parse_https_host(url)
    if host == "localhost":
        raise UnsafeOutboundUrl(f"{field}: localhost is not allowed")
    _check_ip_literal(host)
    return url


async def validate_https_url_resolved(url: str, *, field: str = "url") -> str:
    """Also resolve DNS and reject private/reserved addresses."""
    validate_https_url(url, field=field)
    await _resolve_validated_ip(url)
    return url


async def _resolve_validated_ip(url: str) -> str:
    """Resolve ``url`` and return one validated (public) IP.

    Raises :class:`UnsafeOutboundUrl` if the host cannot be resolved or any
    resolved address is private/reserved.  The caller should pin the returned
    IP for the actual connection to defeat DNS-rebinding (TOCTOU).
    """
    host, port = _parse_https_host(url)
    _check_ip_literal(host)
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            host,
            port or 443,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise UnsafeOutboundUrl(f"cannot resolve hostname {host!r}") from exc
    if not infos:
        raise UnsafeOutboundUrl(f"cannot resolve hostname {host!r}")
    has_fake_ip = False
    for info in infos:
        address = info[4][0]
        if _is_proxy_fake_ip(address):
            has_fake_ip = True
        else:
            _check_resolved_ip(address)
    if has_fake_ip:
        return await _resolve_public_dns(host)
    return infos[0][4][0]


class _PinnedNetworkBackend(AutoBackend):
    """Resolve the validated host to a fixed IP, while preserving SNI.

    Only requests whose host matches ``_target_host`` are pinned to
    ``_pin_ip``; everything else (e.g. redirects) resolves normally so the
    helper never breaks legitimate cross-host redirects.  The original
    hostname is passed to TLS via httpcore's SNI logic, so certificate
    validation is unaffected by the IP pinning.
    """

    def __init__(self, target_host: str, pin_ip: str) -> None:
        super().__init__()
        self._target_host = target_host
        self._pin_ip = pin_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        if host == self._target_host:
            return await super().connect_tcp(
                self._pin_ip,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        return await super().connect_tcp(
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class PinnedIPTransport(httpx.AsyncHTTPTransport):
    """httpx transport that pins the validated IP for ``target_host``.

    Swaps the underlying httpcore network backend so the validated host always
    connects to the validated IP — closing the DNS-rebinding window between
    validation and the actual TCP connection (CWE-918).
    """

    def __init__(self, target_host: str, pin_ip: str) -> None:
        super().__init__()
        self._pool._network_backend = _PinnedNetworkBackend(target_host, pin_ip)


async def _resolve_public_dns(host: str) -> str:
    """Recover a real public IP when a TUN proxy supplies Fake-IP DNS answers.

    Bootstrap DoH at a fixed public IP so the resolver itself does not depend
    on synthetic DNS. Keep TLS hostname verification and never connect to the
    synthetic address, even when the encrypted lookup fails.
    """
    transport = PinnedIPTransport("cloudflare-dns.com", "1.1.1.1")
    addresses: list[str] = []
    try:
        async with httpx.AsyncClient(
            transport=transport, timeout=10.0, trust_env=False, follow_redirects=False
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.get(
                        "https://cloudflare-dns.com/dns-query",
                        params={"name": host, "type": record_type},
                        headers={"Accept": "application/dns-json"},
                    )
                    for record_type in ("A", "AAAA")
                )
            )
        for response in responses:
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("Status") != 0:
                raise UnsafeOutboundUrl(f"public DNS lookup failed for {host!r}")
            for answer in body.get("Answer", []):
                if answer.get("type") in (1, 28):
                    address = str(answer["data"])
                    _check_resolved_ip(address)
                    addresses.append(address)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise UnsafeOutboundUrl(
            f"cannot resolve a public address for {host!r} via encrypted DNS"
        ) from exc
    if not addresses:
        raise UnsafeOutboundUrl(f"cannot resolve a public address for {host!r} via encrypted DNS")
    return addresses[0]


async def safe_request(
    method: str,
    url: str,
    *,
    json: typing.Any | None = None,
    data: typing.Any | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> httpx.Response:
    """Validate, resolve, pin the IP, and perform an outbound HTTPS request.

    URL scheme/host must be https and resolve to a public IP (see
    :func:`validate_https_url_resolved`).  The connection is then pinned to the
    validated IP so a malicious DNS change between validation and connection
    cannot redirect the request to an internal address.
    """
    parsed = urlparse(url)
    host, port = _parse_https_host(url)
    pin_ip = await _resolve_validated_ip(url)
    # Rebuild the request URL from validated components so the outbound call
    # does not reuse the original user-controlled string (CWE-918 / CodeQL).
    netloc = f"{host}:{port}" if port is not None else host
    request_url = urlunparse(("https", netloc, parsed.path or "/", "", parsed.query, ""))
    transport = PinnedIPTransport(host, pin_ip)
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        return await client.request(method, request_url, json=json, data=data, headers=headers)
