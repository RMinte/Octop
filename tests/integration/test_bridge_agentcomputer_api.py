"""Owner-scoped Bridge routes for remote AgentComputer project conversations."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from octop.infra.bridge.http_tunnel import encode_tunnel_response
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/bridge/connections/01REMOTE/agentcomputer/v1"


async def test_bridge_agentcomputer_keeps_owner_bearer_scope_and_safe_json(env: Any) -> None:
    client, server, auth = env
    assert server.services is not None and server.app_runtime is not None
    owner_id = await resolve_user_id(client, auth, "admin")
    server.services.bridge_connection_repo.create(
        connection_id="01REMOTE",
        owner_user_id=owner_id,
        peer_base_url="https://peer.example",
        peer_username="peer",
        display_name="Remote",
    )
    session = MagicMock()
    session.tunnel_request = AsyncMock(
        return_value=encode_tunnel_response(
            status=200,
            headers={"Set-Cookie": "secret=leak", "Authorization": "Bearer leak"},
            body=json.dumps({"items": [], "nextCursor": None}).encode(),
        )
    )
    manager = server.app_runtime.bridge_manager
    assert manager is not None
    with patch.object(manager, "require_session", return_value=session):
        response = await client.get(f"{PREFIX}/projects?limit=5", headers=auth)
        assert response.status_code == 200
        assert response.json() == {"items": [], "nextCursor": None}
        assert response.headers["cache-control"] == "no-store"
        assert "set-cookie" not in response.headers
        assert "authorization" not in response.headers
        session.tunnel_request.assert_awaited_once_with(
            method="GET",
            path="/api/agentcomputer/v1/projects",
            query="limit=5",
            headers=None,
            body=None,
        )

        token = auth["Authorization"].split(" ", 1)[1]
        query_token = await client.get(f"{PREFIX}/projects?access_token={token}")
        assert query_token.status_code == 401
        assert session.tunnel_request.await_count == 1

        other_auth = await create_user(client, auth, username="other")
        other = await client.get(f"{PREFIX}/projects", headers=other_auth)
        assert other.status_code == 404
        assert session.tunnel_request.await_count == 1

        disallowed = await client.get(f"{PREFIX}/users", headers=auth)
        assert disallowed.status_code == 404
        assert session.tunnel_request.await_count == 1

    schema = client._octop_app.openapi()  # type: ignore[attr-defined]
    project_path = "/api/bridge/connections/{connection_id}/agentcomputer/v1/projects"
    assert schema["paths"][project_path]["get"]["summary"]
    assert schema["paths"][project_path]["post"]["requestBody"]["content"]["application/json"]


async def test_bridge_agentcomputer_forwards_exact_command_body_and_peer_error(env: Any) -> None:
    client, server, auth = env
    assert server.services is not None and server.app_runtime is not None
    owner_id = await resolve_user_id(client, auth, "admin")
    server.services.bridge_connection_repo.create(
        connection_id="01REMOTE",
        owner_user_id=owner_id,
        peer_base_url="https://peer.example",
        peer_username="peer",
        display_name="Remote",
    )
    session = MagicMock()
    session.tunnel_request = AsyncMock(
        return_value=encode_tunnel_response(
            status=503,
            headers={"Set-Cookie": "secret=leak"},
            body=(
                b'{"error":{"code":"runtimeUnavailable","retryable":true,'
                b'"reasonCode":"private-token"},"access_token":"secret"}'
            ),
        )
    )
    manager = server.app_runtime.bridge_manager
    assert manager is not None
    command = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "远端项目",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "GLM/glm-5",
            "instructions": "保持中文",
        },
    }
    original = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode()
    with patch.object(manager, "require_session", return_value=session):
        response = await client.post(
            f"{PREFIX}/projects",
            headers={**auth, "Content-Type": "application/json"},
            content=original,
        )
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "runtimeUnavailable", "retryable": True}}
    assert "set-cookie" not in response.headers
    session.tunnel_request.assert_awaited_once_with(
        method="POST",
        path="/api/agentcomputer/v1/projects",
        query="",
        headers={"content-type": "application/json"},
        body=original,
    )

    session.tunnel_request.return_value = encode_tunnel_response(
        status=404,
        headers={},
        body=b'{"detail":"private peer diagnostic"}',
    )
    with patch.object(manager, "require_session", return_value=session):
        missing = await client.get(f"{PREFIX}/projects", headers=auth)
    assert missing.status_code == 404
    assert missing.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
    assert "private peer diagnostic" not in missing.text
