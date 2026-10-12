"""Receipt queries preserve admission facts without enabling command writes."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer.identity import get_or_create_identity
from octop.infra.agentcomputer.models import CommandReceipt
from octop.infra.server import OctopServer
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import auth_header, bootstrap_admin, create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def seed_receipt(server: OctopServer, owner_id: int) -> CommandReceipt:
    """Insert an isolated acceptance fixture; no project or engine is started."""
    assert server.services is not None
    identity = get_or_create_identity(server.paths.agentcomputer_identity)
    receipt = CommandReceipt(
        boxID=identity.boxID,
        commandID=uuid4(),
        acceptedAt=datetime(2026, 10, 12, tzinfo=UTC),
        restoreEpoch=identity.restoreEpoch,
        taskID=uuid4(),
        turnID=uuid4(),
        runID=uuid4(),
        userMessageID=uuid4(),
        assistantMessageID=uuid4(),
        taskRevision=2,
        acceptedSequence=1,
    )
    with server.services.db.transaction() as conn:
        conn.execute(
            "INSERT INTO ac_commands (command_id, owner_id, idempotency_key, operation, "
            "target_id, payload_digest, restore_epoch, receipt_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(receipt.commandID),
                owner_id,
                str(uuid4()),
                "turn.newMessage",
                str(receipt.taskID),
                "sha256:" + "0" * 64,
                receipt.restoreEpoch,
                receipt.model_dump_json(),
                int(receipt.acceptedAt.timestamp() * 1000),
            ),
        )
    return receipt


async def test_own_receipt_is_original_durable_result(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = seed_receipt(server, owner_id)
    assert server.services is not None
    original = server.services.ac_command_repo.get(str(receipt.commandID), owner_id)
    for _ in range(2):
        response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == receipt.model_dump(mode="json")
        assert response.json()["replayed"] is False
        assert response.json()["runState"] == "queued"
        assert str(server.paths.root) not in response.text
    assert server.services.ac_command_repo.get(str(receipt.commandID), owner_id) == original
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ac_turns").fetchone()[0] == 0


async def test_other_owner_and_admin_cannot_read_receipt_or_identity(env) -> None:
    client, server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="receipt-owner")
    other_auth = await create_user(client, admin_auth, username="receipt-other")
    owner_id = await resolve_user_id(client, admin_auth, "receipt-owner")
    receipt = seed_receipt(server, owner_id)
    own = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=owner_auth)
    assert own.status_code == 200
    # Lookup must not read identity unless an owned row exists.
    server.paths.agentcomputer_identity.write_text('{"private-marker":', encoding="utf-8")
    for auth in (other_auth, admin_auth):
        missing = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
        denied = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert denied.headers["cache-control"] == "no-store"


async def test_invalid_identifier_has_safe_scoped_validation_error(env) -> None:
    client, _server, auth = env
    response = await client.get(f"{PREFIX}/commands/private-input-marker", headers=auth)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert "private-input-marker" not in response.text
    legacy = await client.post("/api/auth/login", json={"username": "admin"})
    assert legacy.status_code == 422
    assert "detail" in legacy.json()


async def test_receipt_query_requires_header_bearer(env) -> None:
    client, server, auth = env
    endpoint = f"{PREFIX}/commands/{uuid4()}"
    token = auth["Authorization"].split(" ", 1)[1]
    requests = (
        await client.get(endpoint),
        await client.get(endpoint, params={"access_token": token}),
        await client.get(endpoint, params={"access_token": token}, headers=auth),
    )
    for response in requests:
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    assert not server.paths.agentcomputer_identity.exists()


async def test_receipt_survives_restart_and_keeps_acceptance_epoch(tmp_path: Path) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        owner_id = await resolve_user_id(client, auth, "admin")
        receipt = seed_receipt(server, owner_id)
        first = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert first.status_code == 200
        # Simulate only persisted metadata, not a working restore implementation.
        path = server.paths.agentcomputer_identity
        identity = json.loads(path.read_text(encoding="utf-8"))
        identity["restoreEpoch"] = 2
        path.write_text(json.dumps(identity), encoding="utf-8")
    async with octop_client(home) as (client, _server):
        auth = await auth_header(client)
        second = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert second.status_code == 200
        assert second.json() == first.json() == receipt.model_dump(mode="json")
        capabilities = await client.get(f"{PREFIX}/capabilities", headers=auth)
        assert capabilities.json()["restoreEpoch"] == 2
        assert second.json()["restoreEpoch"] == 1


@pytest.mark.parametrize("invalid", ['{"private-marker":', '{"accepted":true}'])
async def test_invalid_stored_receipt_returns_safe_503_without_replacement(
    env, invalid: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = seed_receipt(server, owner_id)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_commands SET receipt_json = ? WHERE command_id = ?",
            (invalid, str(receipt.commandID)),
        )
    response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "receiptInvalid"}
    }
    assert "private-marker" not in response.text
    assert str(server.paths.root) not in response.text
    stored = server.services.ac_command_repo.get(str(receipt.commandID), owner_id)
    assert stored is not None
    assert stored.receipt_json == invalid


async def test_invalid_identity_returns_safe_503_for_owned_receipt(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = seed_receipt(server, owner_id)
    invalid = b'{"private-marker":'
    path = server.paths.agentcomputer_identity
    path.write_bytes(invalid)
    response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "identityInvalid"}
    }
    assert path.read_bytes() == invalid
    assert "private-marker" not in response.text


async def test_storage_failure_returns_retryable_safe_503(env, monkeypatch) -> None:
    client, _server, auth = env

    def unavailable(*_args: object) -> None:
        raise OSError("private-storage-marker")

    monkeypatch.setattr(agentcomputer, "get_command_receipt", unavailable)
    response = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "storageUnavailable", "retryable": True}}
    assert response.headers["cache-control"] == "no-store"
    assert "private-storage-marker" not in response.text


async def test_database_failure_returns_retryable_safe_503(env, monkeypatch) -> None:
    client, server, auth = env
    assert server.services is not None

    def unavailable(*_args: object) -> None:
        raise sqlite3.OperationalError("private-sql-marker SELECT secret FROM ac_commands")

    monkeypatch.setattr(server.services.ac_command_repo, "get", unavailable)
    response = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "storageUnavailable", "retryable": True}}
    assert response.headers["cache-control"] == "no-store"
    assert "private-sql-marker" not in response.text
    assert "SELECT" not in response.text


async def test_command_writes_remain_unavailable(env) -> None:
    client, server, auth = env
    for path in (f"{PREFIX}/commands", f"{PREFIX}/tasks/{uuid4()}/turns"):
        response = await client.post(path, headers=auth, json={})
        # An unmatched POST can be rejected by the dashboard's static mount.
        assert response.status_code in (404, 405)
        expected_code = "resourceNotFound" if response.status_code == 404 else "invalidRequest"
        assert response.json()["error"]["code"] == expected_code
    response = await client.post(f"{PREFIX}/commands/{uuid4()}", headers=auth, json={})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalidRequest"
    assert response.headers["allow"] == "GET"
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_commands").fetchone()[0] == 0


async def test_openapi_documents_receipt_as_immutable_acceptance(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"][f"{PREFIX}/commands/{{commandID}}"]["get"]
        assert operation["summary"]
        assert operation["tags"] == ["agentcomputer"]
        assert operation["security"] == [{"BearerAuth": []}]
        parameter = next(p for p in operation["parameters"] if p["in"] == "path")
        assert parameter["name"] == "commandID"
        assert parameter["schema"]["format"] == "uuid"
        assert "current Run state" in operation["description"]
        response = operation["responses"]["200"]["content"]["application/json"]
        assert {item["$ref"].rsplit("/", 1)[1] for item in response["schema"]["anyOf"]} == {
            "CommandReceipt",
            "ProjectCreateReceipt",
            "TaskCreateReceipt",
        }
        example = response["example"]
        assert CommandReceipt.model_validate(example).runState == "queued"
        assert example["accepted"] is True
        assert example["replayed"] is False
        for status in ("401", "403", "404", "422", "503"):
            error = operation["responses"][status]["content"]["application/json"]["schema"]
            assert error["$ref"].endswith("/ProtocolErrorEnvelope")
        assert "post" not in schema["paths"][f"{PREFIX}/commands/{{commandID}}"]
        assert (await client.get("/api/docs")).status_code == 200
