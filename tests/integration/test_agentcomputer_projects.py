"""Local project intents never grant project readiness or model execution."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer.commands import CommandAdmissionError, StorageDataError
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.agentcomputer.limits import MAX_COMMAND_BYTES
from octop.infra.agentcomputer.models import ProjectCreateReceipt, ProjectView
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import auth_header, bootstrap_admin, create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"
ENDPOINT = f"{PREFIX}/projects"


def command(**changes: object) -> dict[str, object]:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "projectID": str(uuid4()),
        "name": "Local project 中文",
        "configuration": {
            "schemaVersion": 1,
            "modelRef": "not-configured/no-model",
            "instructions": "使用中文回答",
        },
        **changes,
    }


def encoded(body: dict[str, object]) -> bytes:
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    for offset in range(0, len(data), 8192):
        yield data[offset : offset + 8192]


async def test_project_receipt_is_pending_while_current_preparation_fails_without_provider(
    env, monkeypatch
) -> None:
    client, server, auth = env
    assert server.app_runtime is not None
    no_start = AsyncMock(side_effect=AssertionError("project must not start a harness"))
    monkeypatch.setattr(server.app_runtime.agent_registry, "_start_agent", no_start)
    body = command(name="  Local project 中文  ")
    response = await client.post(ENDPOINT, headers=auth, json=body)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    receipt = ProjectCreateReceipt.model_validate(response.json())
    assert str(receipt.projectID) == body["projectID"]
    assert receipt.preparationStateAtAcceptance == "pending"
    assert receipt.accepted is True and receipt.replayed is False
    current = await client.get(f"{ENDPOINT}/{receipt.projectID}", headers=auth)
    assert current.status_code == 200
    assert current.headers["cache-control"] == "no-store"
    view = ProjectView.model_validate(current.json())
    assert view.name == body["name"]
    assert view.workspaceID == receipt.workspaceID
    assert view.preparationID == receipt.preparationID
    assert view.preparationState == "failed"
    assert view.reasonCode == "providerUnavailable"
    assert view.steps.agent == view.steps.workspace == "verified"
    assert view.executionReady is False
    assert view.projectRevision == receipt.projectRevision == 1
    assert str(server.paths.root) not in response.text + current.text
    assert not {"defaultAgentID", "plannedAgentID", "serverRoot", "backend"} & current.json().keys()
    no_start.assert_not_awaited()
    assert server.services is not None
    owner = await resolve_user_id(client, auth, "admin")
    row = server.services.ac_project_repo.get(str(receipt.projectID), owner)
    assert row is not None and row.default_agent_id is not None
    agent = server.services.agent_repo.get(row.default_agent_id)
    assert agent is not None and agent.last_state == "stopped"
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ac_tasks").fetchone()[0] == 0


async def test_project_replay_keeps_original_ack_and_resources(env) -> None:
    client, server, auth = env
    body = command()
    first = await client.post(ENDPOINT, headers=auth, json=body)
    assert first.status_code == 201
    retry = {**body, "commandID": str(uuid4())}
    repeated = await client.post(ENDPOINT, headers=auth, json=retry)
    assert repeated.status_code == 201
    assert repeated.json() == {**first.json(), "replayed": True}
    original_id = first.json()["commandID"]
    queried = await client.get(f"{PREFIX}/commands/{original_id}", headers=auth)
    assert queried.status_code == 200
    assert queried.json() == first.json()
    assert (
        await client.get(f"{PREFIX}/commands/{retry['commandID']}", headers=auth)
    ).status_code == 404
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ac_commands").fetchone()[0] == 1


@pytest.mark.parametrize("collision", ["key", "command", "project"])
async def test_project_conflicts_do_not_register_second_intent(env, collision: str) -> None:
    client, server, auth = env
    original = command()
    assert (await client.post(ENDPOINT, headers=auth, json=original)).status_code == 201
    if collision == "key":
        conflicting = {**original, "name": "Changed"}
    elif collision == "command":
        conflicting = {**original, "idempotencyKey": str(uuid4())}
    else:
        conflicting = {**original, "commandID": str(uuid4()), "idempotencyKey": str(uuid4())}
    response = await client.post(ENDPOINT, headers=auth, json=conflicting)
    assert response.status_code == 409
    code = "resourceConflict" if collision == "project" else "idempotencyConflict"
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 1


async def test_project_owner_isolation_includes_admin_and_shared_keys(env) -> None:
    client, _server, admin = env
    alice = await create_user(client, admin, username="project-alice")
    bob = await create_user(client, admin, username="project-bob")
    original = command(idempotencyKey="shared-project-key")
    first = await client.post(ENDPOINT, headers=alice, json=original)
    assert first.status_code == 201
    for auth in (bob, admin):
        denied = await client.get(f"{ENDPOINT}/{original['projectID']}", headers=auth)
        missing = await client.get(f"{ENDPOINT}/{uuid4()}", headers=auth)
        assert denied.status_code == missing.status_code == 404
        assert (
            denied.json()
            == missing.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert denied.headers["cache-control"] == "no-store"
    second = await client.post(
        ENDPOINT, headers=bob, json=command(idempotencyKey="shared-project-key")
    )
    assert second.status_code == 201
    assert second.json()["projectID"] != first.json()["projectID"]


async def test_project_epoch_refused_before_replay_but_old_receipt_readable(env) -> None:
    client, server, auth = env
    body = command()
    first = await client.post(ENDPOINT, headers=auth, json=body)
    assert first.status_code == 201
    path = server.paths.agentcomputer_identity
    identity = json.loads(path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] = 2
    path.write_text(json.dumps(identity), encoding="utf-8")
    replay = await client.post(ENDPOINT, headers=auth, json=body)
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "restoreEpochConflict"
    queried = await client.get(f"{PREFIX}/commands/{body['commandID']}", headers=auth)
    assert queried.status_code == 200
    assert queried.json() == first.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"ownerID": 2},
        {"hostPath": "private-host-path-marker"},
        {"backend": {"type": "local_shell"}},
        {"expectedRestoreEpoch": True},
        {"expectedRestoreEpoch": "1"},
        {"projectID": "private-invalid-id-marker"},
        {"name": "   "},
        {"name": "private-control-marker\n"},
        {"name": "a" * 129},
        {"configuration": {"modelRef": "a" * 257, "instructions": ""}},
        {"configuration": {"modelRef": "missing/model", "instructions": "a" * 8193}},
    ],
)
async def test_project_command_rejects_untrusted_source_and_invalid_types(env, changes) -> None:
    client, server, auth = env
    response = await client.post(ENDPOINT, headers=auth, json=command(**changes))
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert "private-" not in response.text
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0


async def test_streamed_body_at_limit_is_accepted(env) -> None:
    client, _server, auth = env
    raw = encoded(command())
    raw += b" " * (MAX_COMMAND_BYTES - len(raw))
    response = await client.post(
        ENDPOINT, headers={**auth, "Content-Type": "application/json"}, content=chunks(raw)
    )
    assert response.status_code == 201


@pytest.mark.parametrize("declared_size", [None, "1"])
async def test_actual_stream_over_limit_rejected_before_admission(
    env, declared_size: str | None
) -> None:
    client, server, auth = env
    headers = {**auth, "Content-Type": "application/json"}
    if declared_size is not None:
        headers["Content-Length"] = declared_size
    raw = encoded(command())
    raw += b" " * (MAX_COMMAND_BYTES + 1 - len(raw))
    response = await client.post(ENDPOINT, headers=headers, content=chunks(raw))
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": "payloadLimitExceeded", "retryable": False}}
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ac_commands").fetchone()[0] == 0


async def test_declared_length_cannot_change_actual_small_body(env) -> None:
    client, _server, auth = env
    response = await client.post(
        ENDPOINT,
        headers={
            **auth,
            "Content-Type": "application/json",
            "Content-Length": str(MAX_COMMAND_BYTES + 1),
        },
        content=encoded(command()),
    )
    assert response.status_code == 201


async def test_authentication_precedes_body_limit(env) -> None:
    client, _server, _auth = env
    response = await client.post(ENDPOINT, content=chunks(b"x" * (MAX_COMMAND_BYTES + 1)))
    assert response.status_code == 401


async def test_setup_precedes_body_limit(app_client) -> None:
    setup_client, _server, _home = app_client
    before_setup = await setup_client.post(ENDPOINT, content=b"x" * (MAX_COMMAND_BYTES + 1))
    assert before_setup.status_code == 503
    assert before_setup.json()["error"]["reasonCode"] == "setupRequired"


async def test_body_limit_does_not_change_legacy_or_similar_prefix(env) -> None:
    client, _server, auth = env
    legacy = await client.post("/api/auth/login", content=b"x" * (MAX_COMMAND_BYTES + 1))
    assert legacy.status_code == 422
    assert "detail" in legacy.json()
    unrelated = await client.post(
        "/api/agentcomputer/v10/projects", headers=auth, content=b"x" * (MAX_COMMAND_BYTES + 1)
    )
    assert unrelated.status_code != 413


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
async def test_limit_covers_all_versioned_mutating_paths(env, method: str) -> None:
    client, _server, auth = env
    response = await client.request(
        method,
        f"{PREFIX}/unimplemented",
        headers=auth,
        content=chunks(b"x" * (MAX_COMMAND_BYTES + 1)),
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "code,status",
    [
        ("resourceConflict", 409),
        ("idempotencyConflict", 409),
        ("revisionConflict", 409),
        ("restoreEpochConflict", 409),
        ("runtimeUnavailable", 503),
        ("capabilityUnsupported", 422),
        ("accessDenied", 403),
    ],
)
async def test_project_admission_refusals_are_safe(env, monkeypatch, code, status) -> None:
    client, _server, auth = env

    async def refused(*_args, **_kwargs):
        raise CommandAdmissionError(code, reason="private-host-path-marker")

    monkeypatch.setattr(agentcomputer, "create_project", refused)
    response = await client.post(ENDPOINT, headers=auth, json=command())
    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "retryable": False}}
    assert "private-host-path-marker" not in response.text


@pytest.mark.parametrize("failure", [IdentityError, StorageDataError, OSError])
async def test_project_storage_failures_are_safe(env, monkeypatch, failure) -> None:
    client, _server, auth = env

    async def refused(*_args, **_kwargs):
        raise failure("private-storage-marker")

    monkeypatch.setattr(agentcomputer, "create_project", refused)
    response = await client.post(ENDPOINT, headers=auth, json=command())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storageUnavailable"
    assert "private-storage-marker" not in response.text


async def test_project_and_receipt_survive_server_restart(tmp_path: Path) -> None:
    home = tmp_path / "control"
    body = command()
    async with octop_client(home) as (client, _server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        first = await client.post(ENDPOINT, headers=auth, json=body)
        assert first.status_code == 201
        view = await client.get(f"{ENDPOINT}/{body['projectID']}", headers=auth)
        assert view.status_code == 200
    async with octop_client(home) as (client, _server):
        auth = await auth_header(client)
        queried = await client.get(f"{PREFIX}/commands/{body['commandID']}", headers=auth)
        assert queried.status_code == 200
        assert queried.json() == first.json()
        current = await client.get(f"{ENDPOINT}/{body['projectID']}", headers=auth)
        assert current.json() == view.json()


async def test_openapi_distinguishes_project_ack_from_current_status(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        post = schema["paths"][ENDPOINT]["post"]
        get = schema["paths"][f"{ENDPOINT}/{{projectID}}"]["get"]
        assert post["security"] == get["security"] == [{"BearerAuth": []}]
        request = post["requestBody"]["content"]["application/json"]["schema"]
        assert request["$ref"].endswith("/ProjectCreateCommand")
        receipt = post["responses"]["201"]["content"]["application/json"]
        assert receipt["schema"]["$ref"].endswith("/ProjectCreateReceipt")
        assert (
            ProjectCreateReceipt.model_validate(receipt["example"]).preparationStateAtAcceptance
            == "pending"
        )
        current = get["responses"]["200"]["content"]["application/json"]
        assert current["schema"]["$ref"].endswith("/ProjectView")
        assert ProjectView.model_validate(current["example"]).executionReady is False
        for status in ("401", "403", "409", "413", "422", "503"):
            error = post["responses"][status]["content"]["application/json"]["schema"]
            assert error["$ref"].endswith("/ProtocolErrorEnvelope")
        assert "get" not in schema["paths"][ENDPOINT]
        assert (await client.get("/api/docs")).status_code == 200
