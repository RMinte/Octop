"""Unit tests for peer URL normalization / SSRF guards."""

from __future__ import annotations

import pytest

from octop.infra.bridge.peer_auth import normalize_peer_base_url
from octop.infra.errors import ErrorCode, OctopError


def test_normalize_accepts_https_host(monkeypatch: pytest.MonkeyPatch) -> None:
    from octop.infra.bridge import peer_auth

    monkeypatch.setattr(
        peer_auth.socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", ("203.0.113.1", 443))]
    )
    assert normalize_peer_base_url("https://demo.octop.chat/") == "https://demo.octop.chat"


def test_normalize_rejects_metadata_ip() -> None:
    with pytest.raises(OctopError) as ei:
        normalize_peer_base_url("http://169.254.169.254/latest/meta-data")
    assert ei.value.code == ErrorCode.BRIDGE_PEER_UNREACHABLE


def test_normalize_rejects_non_http() -> None:
    with pytest.raises(OctopError) as ei:
        normalize_peer_base_url("ftp://peer.example")
    assert ei.value.code == ErrorCode.BRIDGE_PEER_UNREACHABLE
