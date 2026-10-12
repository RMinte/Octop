"""Protocol discovery cannot grant device or task-execution capabilities."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from octop.infra.auth.tokens import sign_token
from octop.infra.server import OctopServer
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import auth_header, bootstrap_admin, create_user, resolve_user_id

ENDPOINT = "/api/agentcomputer/v1/capabilities"


async def test_setup_is_required_before_identity_discovery(app_client) -> None:
    client, server, _home = app_client
    response = await client.get(ENDPOINT)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "runtimeUnavailable", "retryable": False, "reasonCode": "setupRequired"}
    }
    assert not server.paths.agentcomputer_identity.exists()
    # Existing Octop callers keep their original setup envelope.
    assert (await client.get("/api/auth/me")).json() == {"setup_required": True}


@pytest.mark.parametrize("authorization", [None, "", "Basic example", "Bearer", "Bearer invalid"])
async def test_missing_or_invalid_bearer_cannot_discover_identity(
    env: tuple[httpx.AsyncClient, OctopServer, dict[str, str]], authorization: str | None
) -> None:
    client, server, _auth = env
    headers = {} if authorization is None else {"Authorization": authorization}
    response = await client.get(ENDPOINT, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["cache-control"] == "no-store"
    assert not server.paths.agentcomputer_identity.exists()


async def test_query_credentials_rejected_only_on_versioned_surface(env) -> None:
    client, _server, auth = env
    token = auth["Authorization"].split(" ", 1)[1]
    for headers in ({}, auth):
        response = await client.get(ENDPOINT, params={"access_token": token}, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authenticationRequired"
    legacy = await client.get("/api/auth/me", params={"access_token": token})
    assert legacy.status_code == 200
    assert legacy.json()["username"] == "admin"


async def test_expired_and_disabled_users_cannot_discover_identity(env) -> None:
    client, server, admin_auth = env
    assert server.services is not None
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    user_id = await resolve_user_id(client, admin_auth, "admin")
    expired = sign_token(secret, sub=user_id, uname="admin", role="admin", ttl_seconds=-1)
    response = await client.get(ENDPOINT, headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "credentialExpired"
    auth = await create_user(client, admin_auth, username="reader")
    reader_id = await resolve_user_id(client, admin_auth, "reader")
    disabled = await client.patch(
        f"/api/users/{reader_id}", headers=admin_auth, json={"disabled": True}
    )
    assert disabled.status_code == 200
    response = await client.get(ENDPOINT, headers=auth)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "accessDenied"
    assert not server.paths.agentcomputer_identity.exists()


async def test_discovery_reports_actual_server_and_no_execution_capabilities(env) -> None:
    client, server, auth = env
    before = datetime.now(UTC)
    response = await client.get(ENDPOINT, headers=auth)
    after = datetime.now(UTC)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert str(UUID(body["boxID"])) == body["boxID"]
    assert body["protocolVersion"] == body["minimumClientProtocolVersion"] == 1
    assert body["deviceID"] is None
    assert body["authenticationMode"] == "octopUserSession"
    assert body["restoreEpoch"] == 1
    assert body["capabilityRevision"] == 5
    assert body["supportedPermissionModes"] == []
    assert body["capabilities"]
    assert body["capabilities"]["commandReceipts"] == {
        "supported": True,
        "available": True,
        "reasonCode": None,
    }
    assert body["capabilities"]["projects"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert body["capabilities"]["tasks"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert body["capabilities"]["runs"] == {
        "supported": True,
        "available": False,
        "reasonCode": "executionUnavailable",
    }
    assert all(
        item == {"supported": False, "available": False, "reasonCode": "notImplemented"}
        for name, item in body["capabilities"].items()
        if name not in {"commandReceipts", "projects", "tasks", "runs"}
    )
    assert body["limits"]["maxCommandBytes"] == 65536
    assert all(limit is None for name, limit in body["limits"].items() if name != "maxCommandBytes")
    assert before <= datetime.fromisoformat(body["serverTime"].replace("Z", "+00:00")) <= after
    assert server.services is not None
    assert body["timezone"] == server.services.config.default_timezone
    assert str(server.paths.root) not in response.text
    assert auth["Authorization"] not in response.text
    repeated = await client.get(ENDPOINT, headers=auth)
    assert repeated.json()["boxID"] == body["boxID"]


async def test_identity_persists_across_actual_server_restart(tmp_path: Path) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, _server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        first = (await client.get(ENDPOINT, headers=auth)).json()
    async with octop_client(home) as (client, _server):
        auth = await auth_header(client)
        second = (await client.get(ENDPOINT, headers=auth)).json()
    assert first["boxID"] == second["boxID"]
    assert first["restoreEpoch"] == second["restoreEpoch"] == 1


async def test_invalid_identity_stays_invalid_without_data_disclosure(env) -> None:
    client, server, auth = env
    assert (await client.get(ENDPOINT, headers=auth)).status_code == 200
    path = server.paths.agentcomputer_identity
    invalid = b'{"private-marker": '
    path.write_bytes(invalid)
    response = await client.get(ENDPOINT, headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "identityInvalid"}
    }
    assert path.read_bytes() == invalid
    assert "private-marker" not in response.text
    assert str(path) not in response.text


async def test_unimplemented_routes_are_explicit_errors(env) -> None:
    client, _server, auth = env
    response = await client.get("/api/agentcomputer/v1/tasks", headers=auth)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resourceNotFound"
    response = await client.post(ENDPOINT, headers=auth, json={})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalidRequest"
    assert response.headers["allow"] == "GET"


async def test_openapi_documents_typed_authenticated_discovery(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"][ENDPOINT]["get"]
        assert operation["summary"]
        assert operation["tags"] == ["agentcomputer"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert not any(p["name"] == "access_token" for p in operation.get("parameters", []))
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert response_schema["$ref"].endswith("/AgentComputerCapabilitiesResponse")
        model = schema["components"]["schemas"][response_schema["$ref"].rsplit("/", 1)[1]]
        assert model["properties"]["deviceID"]["anyOf"] == [
            {"type": "string", "format": "uuid"},
            {"type": "null"},
        ]
        example = operation["responses"]["200"]["content"]["application/json"]["example"]
        assert example["deviceID"] is None
        assert example["supportedPermissionModes"] == []
        assert example["capabilityRevision"] == 5
        assert example["capabilities"]["commandReceipts"] == {
            "supported": True,
            "available": True,
            "reasonCode": None,
        }
        assert example["capabilities"]["projects"] == {
            "supported": True,
            "available": False,
            "reasonCode": "executionUnavailable",
        }
        assert example["capabilities"]["tasks"] == {
            "supported": True,
            "available": False,
            "reasonCode": "executionUnavailable",
        }
        assert example["capabilities"]["runs"] == {
            "supported": True,
            "available": False,
            "reasonCode": "executionUnavailable",
        }
        assert example["limits"]["maxCommandBytes"] == 65536
        assert all(
            not item["available"]
            for name, item in example["capabilities"].items()
            if name != "commandReceipts"
        )
        for status in ("401", "403", "503"):
            error_schema = operation["responses"][status]["content"]["application/json"]["schema"]
            assert error_schema["$ref"].endswith("/ProtocolErrorEnvelope")
        docs = await client.get("/api/docs")
        assert docs.status_code == 200
        assert "/api/openapi.json" in docs.text
