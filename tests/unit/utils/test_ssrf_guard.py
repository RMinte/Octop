"""Unit tests for SSRF guard (infra/utils/ssrf_guard.py)."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import ssl
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import AsyncNetworkStream

from octop.infra.utils import ssrf_guard
from octop.infra.utils.ssrf_guard import (
    UnsafeOutboundUrl,
    host_allowed_for_issuer,
    is_private_or_local_host,
    issuer_base_domain,
    safe_request,
    validate_https_url,
    validate_https_url_resolved,
)


def _mock_dns(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> AsyncMock:
    infos = [
        (
            socket.AF_INET if ipaddress.ip_address(address).version == 4 else socket.AF_INET6,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            (address, 443),
        )
        for address in addresses
    ]
    resolver = AsyncMock(return_value=infos)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    return resolver


def _mock_doh(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[tuple[str, str]]:
    connections: list[tuple[str, str]] = []

    def transport(target_host: str, pin_ip: str) -> httpx.MockTransport:
        connections.append((target_host, pin_ip))
        return httpx.MockTransport(handler)

    monkeypatch.setattr(ssrf_guard, "PinnedIPTransport", transport)
    return connections


def test_issuer_base_domain() -> None:
    assert issuer_base_domain("https://mcp.notion.com") == "notion.com"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("10.0.0.1", True),
        ("192.168.1.1", True),
        ("host.docker.internal", True),
        ("nas.local", True),
        ("mcp.example.com", False),
        ("8.8.8.8", False),
    ],
)
def test_is_private_or_local_host(host: str, expected: bool) -> None:
    assert is_private_or_local_host(host) is expected


@pytest.mark.parametrize(
    ("host", "issuer", "allowed"),
    [
        ("mcp.notion.com", "https://mcp.notion.com", True),
        ("api.notion.com", "https://mcp.notion.com", True),
        ("evil.com", "https://mcp.notion.com", False),
        ("notion.com.evil.com", "https://mcp.notion.com", False),
    ],
)
def test_host_allowed_for_issuer(host: str, issuer: str, allowed: bool) -> None:
    assert host_allowed_for_issuer(host, issuer) is allowed


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.notion.com/token",
        "https://127.0.0.1/token",
        "https://10.0.0.1/token",
        "https://100.64.0.1/token",
        "https://localhost/token",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_validate_https_url_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(UnsafeOutboundUrl):
        validate_https_url(url, field="token_endpoint")


def test_validate_https_url_accepts_public_host() -> None:
    assert (
        validate_https_url("https://mcp.notion.com/.well-known/oauth-authorization-server")
        == "https://mcp.notion.com/.well-known/oauth-authorization-server"
    )


@pytest.mark.asyncio
async def test_public_system_dns_does_not_use_encrypted_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, "104.20.26.229", "2606:4700:10::6814:1ae5")
    connections = _mock_doh(monkeypatch, lambda request: httpx.Response(500))

    assert await validate_https_url_resolved("https://mcp.openalex.org/mcp")
    assert connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address",
    [
        "198.18.0.0",
        "198.19.255.255",
        "::ffff:198.18.1.53",
        "::ffff:0:c612:135",
    ],
)
async def test_fake_dns_uses_public_encrypted_answers(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _mock_dns(monkeypatch, address)
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answers = (
            [
                {"type": 5, "data": "public.example.org"},
                {"type": 1, "data": "104.20.26.229"},
            ]
            if request.url.params["type"] == "A"
            else []
        )
        return httpx.Response(200, json={"Status": 0, "Answer": answers})

    _mock_doh(monkeypatch, respond)

    assert await ssrf_guard._resolve_validated_ip("https://mcp.openalex.org/mcp") == "104.20.26.229"
    assert {request.url.params["type"] for request in requests} == {"A", "AAAA"}
    assert all(request.url.params["name"] == "mcp.openalex.org" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ("198.18.1.53", "10.0.0.1"),
        ("127.0.0.1", "198.18.1.53"),
        ("198.18.1.53", "::ffff:127.0.0.1"),
        ("198.18.1.53", "169.254.169.254"),
        ("2001:db8::c612:135",),
        ("::ffff:0:a00:1",),
        ("100.64.0.1",),
        ("198.18.1.53", "100.127.255.255"),
    ],
)
async def test_private_dns_is_not_accepted_as_proxy_dns(
    monkeypatch: pytest.MonkeyPatch, addresses: tuple[str, ...]
) -> None:
    _mock_dns(monkeypatch, *addresses)
    connections = _mock_doh(monkeypatch, lambda request: httpx.Response(500))

    with pytest.raises(UnsafeOutboundUrl, match="private or reserved"):
        await validate_https_url_resolved("https://mcp.openalex.org/mcp")

    assert connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["198.18.1.53", "[::ffff:198.18.1.53]", "[::ffff:0:c612:135]"])
async def test_literal_fake_address_cannot_trigger_dns_fallback(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    resolver = _mock_dns(monkeypatch, "104.20.26.229")
    connections = _mock_doh(monkeypatch, lambda request: httpx.Response(500))

    with pytest.raises(UnsafeOutboundUrl, match="private or reserved"):
        await safe_request("GET", f"https://{host}/mcp")

    resolver.assert_not_awaited()
    assert connections == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"Status": 0},
        {"Status": 0, "Answer": []},
        {"Status": 3},
        {"Status": 0, "Answer": [{"type": 5, "data": "public.example.org"}]},
        {
            "Status": 0,
            "Answer": [
                {"type": 1, "data": "104.20.26.229"},
                {"type": 1, "data": "10.0.0.1"},
            ],
        },
        {"Status": 0, "Answer": [{"type": 28, "data": "::ffff:127.0.0.1"}]},
        {"Status": 0, "Answer": [{"type": 1, "data": "198.18.1.53"}]},
        {"Status": 0, "Answer": [{"type": 1, "data": "100.64.0.1"}]},
        {"Status": 0, "Answer": [{"type": 1, "data": "100.127.255.255"}]},
        {"Status": 0, "Answer": [{"type": 1, "data": "not-an-address"}]},
    ],
)
async def test_encrypted_dns_cannot_authorize_unsafe_or_empty_answers(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    _mock_dns(monkeypatch, "198.18.1.53")
    _mock_doh(monkeypatch, lambda request: httpx.Response(200, json=body))

    with pytest.raises(UnsafeOutboundUrl, match="public address"):
        await validate_https_url_resolved("https://mcp.openalex.org/mcp")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 503])
async def test_encrypted_dns_http_failure_cannot_fall_back_to_fake_address(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _mock_dns(monkeypatch, "198.18.1.53")
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://127.0.0.1/dns-query"})

    _mock_doh(monkeypatch, respond)

    with pytest.raises(UnsafeOutboundUrl, match="public address"):
        await safe_request("GET", "https://mcp.openalex.org/mcp")

    assert all(request.url.host == "cloudflare-dns.com" for request in requests)


@pytest.mark.asyncio
async def test_encrypted_dns_timeout_cannot_fall_back_to_fake_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_dns(monkeypatch, "198.18.1.53")

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("resolver unavailable", request=request)

    connections = _mock_doh(monkeypatch, fail)

    with pytest.raises(UnsafeOutboundUrl, match="public address"):
        await safe_request("GET", "https://mcp.openalex.org/mcp")

    assert connections == [("cloudflare-dns.com", "1.1.1.1")]


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy_dns", [False, True], ids=["system-dns", "fake-ip-dns"])
@pytest.mark.parametrize(
    ("hostname", "ascii_hostname", "port"),
    [
        ("mcp.openalex.org", "mcp.openalex.org", 443),
        ("bücher.example", "xn--bcher-kva.example", 8443),
    ],
)
async def test_requests_pin_public_ips_and_preserve_tls_hostnames(
    monkeypatch: pytest.MonkeyPatch,
    proxy_dns: bool,
    hostname: str,
    ascii_hostname: str,
    port: int,
) -> None:
    addresses = ("198.18.1.53", "::ffff:0:c612:135") if proxy_dns else ("104.20.26.229",)
    resolver = _mock_dns(monkeypatch, *addresses)
    connections: list[tuple[str, int]] = []
    tls_hostnames: list[str] = []
    requests: list[bytes] = []

    class RecordingStream(AsyncNetworkStream):
        def __init__(self) -> None:
            self.request = b""
            self.responded = False

        async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
            if self.responded:
                return b""
            self.responded = True
            requests.append(self.request)
            if b"/dns-query?" in self.request:
                answers = (
                    [{"type": 28, "data": "2606:4700:10::6814:1ae5"}]
                    if b"type=AAAA" in self.request
                    else [{"type": 1, "data": "104.20.26.229"}]
                )
                body = json.dumps({"Status": 0, "Answer": answers}).encode()
            else:
                body = b'{"ok": true}'
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Connection: close\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )

        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            self.request += buffer

        async def aclose(self) -> None:
            pass

        async def start_tls(
            self,
            ssl_context: ssl.SSLContext,
            server_hostname: str | None = None,
            timeout: float | None = None,
        ) -> RecordingStream:
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED
            assert ssl_context.check_hostname
            assert server_hostname is not None
            tls_hostnames.append(server_hostname)
            return self

        def get_extra_info(self, info: str) -> Any:
            return None

    async def connect_tcp(
        self: AutoBackend, host: str, port: int, **kwargs: Any
    ) -> RecordingStream:
        connections.append((host, port))
        return RecordingStream()

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect_tcp)

    response = await safe_request("GET", f"https://{hostname}:{port}/mcp?example=true")

    assert response.json() == {"ok": True}
    resolver.assert_awaited_once_with(
        ascii_hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    doh_connections = 2 if proxy_dns else 0
    assert connections.count(("1.1.1.1", 443)) == doh_connections
    assert connections.count(("104.20.26.229", port)) == 1
    assert len(connections) == doh_connections + 1
    assert tls_hostnames.count("cloudflare-dns.com") == doh_connections
    assert tls_hostnames.count(ascii_hostname) == 1
    assert all(f"name={ascii_hostname}".encode() in request for request in requests[:-1])
    assert b"GET /mcp?example=true HTTP/1.1\r\n" in requests[-1]
    authority = ascii_hostname if port == 443 else f"{ascii_hostname}:{port}"
    assert f"Host: {authority}\r\n".encode() in requests[-1]
