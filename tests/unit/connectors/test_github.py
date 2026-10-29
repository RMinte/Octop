"""GitHub PAT connections use the official remote MCP transport."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from octop.config import OctopConfig
from octop.infra.connectors import probe
from octop.infra.connectors.builder import build_http_mcp_spec, validate_create_credentials
from octop.infra.connectors.catalog import get_catalog_entry, is_mcp_oauth_remote


def test_github_catalog_uses_pat_without_dynamic_oauth() -> None:
    entry = get_catalog_entry("github")
    assert entry is not None
    assert entry.auth_kind == "personal_token"
    assert entry.mcp_mode == "remote"
    assert entry.remote_transport == "streamable_http"
    assert entry.mcp_url == "https://api.githubcopilot.com/mcp/"
    assert not is_mcp_oauth_remote(entry)
    assert entry.oauth_issuer is None


@pytest.mark.parametrize("credentials", [{}, {"token": ""}, {"token": " \n "}])
def test_github_rejects_missing_pat(credentials: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="token is required"):
        validate_create_credentials("github", credentials)


def test_github_remote_spec_uses_official_endpoint_and_bearer_pat() -> None:
    entry = get_catalog_entry("github")
    assert entry is not None
    credentials = validate_create_credentials("github", {"token": " github-test-pat "})
    spec = build_http_mcp_spec(
        entry=entry,
        instance_id="github-test-instance",
        creds=credentials,
        config=OctopConfig(),
    )
    assert spec["transport"] == "http"
    assert spec["url"] == "https://api.githubcopilot.com/mcp/"
    assert spec["headers"]["Authorization"] == "Bearer github-test-pat"
    assert spec["headers"]["Accept"] == "application/json, text/event-stream"


async def test_github_probe_uses_streamable_http_instead_of_raw_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = get_catalog_entry("github")
    assert entry is not None
    result = {
        "ok": True,
        "tool_count": 1,
        "tools": [{"name": "get_me", "description": "Get the authenticated user"}],
    }
    streamable = AsyncMock(return_value=result)
    raw = AsyncMock(side_effect=AssertionError("GitHub must use the MCP SDK transport"))
    monkeypatch.setattr(probe, "probe_streamable_http_mcp", streamable)
    monkeypatch.setattr(probe, "safe_request", raw)

    response = await probe.probe_connector(
        entry,
        {"token": "github-test-pat"},
        instance_id="github-test-instance",
        config=OctopConfig(),
    )

    assert response == result
    streamable.assert_awaited_once()
    args, kwargs = streamable.await_args
    assert args[0] == "https://api.githubcopilot.com/mcp/"
    assert args[1]["Authorization"] == "Bearer github-test-pat"
    assert kwargs == {"kind": "github"}
    raw.assert_not_awaited()
