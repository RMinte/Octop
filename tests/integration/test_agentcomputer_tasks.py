"""Read-only task metadata uses explicitly prepared, non-executing fixtures."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer.commands import StorageDataError, StorageUnavailableError
from octop.infra.agentcomputer.models import TaskView
from tests.support.agentcomputer import register_ready_task as registered_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


async def test_task_query_reads_mapping_without_execution_or_internal_ids(env, monkeypatch) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    assert server.app_runtime is not None
    no_start = AsyncMock(side_effect=AssertionError("task metadata must not start a model"))
    monkeypatch.setattr(server.app_runtime.agent_registry, "_start_agent", no_start)
    receipt = await registered_task(client, server, auth, owner)
    response = await client.get(f"{PREFIX}/tasks/{receipt.taskID}", headers=auth)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    view = TaskView.model_validate(body)
    assert view.taskID == receipt.taskID
    assert view.projectID == receipt.projectID
    assert view.sessionID == receipt.sessionID
    assert view.title == "  Fixture task 中文  "
    assert view.taskRevision == 1 and view.preparationState == "ready"
    assert view.executionReady is False
    assert receipt.preparationStateAtAcceptance == "ready"
    assert receipt.executionReady is False
    assert set(body) == {
        "taskID",
        "projectID",
        "sessionID",
        "title",
        "taskRevision",
        "preparationState",
        "executionReady",
    }
    assert str(server.paths.root) not in response.text
    no_start.assert_not_awaited()
    assert server.services is not None
    with server.services.db.connect() as conn:
        for table in ("ac_turns", "ac_runs", "ac_events"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_task_receipt_query_keeps_complete_original_facts(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    receipt = await registered_task(client, server, auth, owner)
    assert server.services is not None
    stored = server.services.ac_command_repo.get(str(receipt.commandID), owner)
    assert stored is not None
    original = stored.receipt_json
    for _ in range(2):
        response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert response.status_code == 200
        assert response.json() == receipt.model_dump(mode="json")
        assert response.json()["operation"] == "task.create"
        assert response.json()["replayed"] is False
        assert response.json()["executionReady"] is False
    stored = server.services.ac_command_repo.get(str(receipt.commandID), owner)
    assert stored is not None and stored.receipt_json == original


async def test_task_owner_isolation_includes_admin_and_unknown_tasks(env) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="task-alice")
    bob = await create_user(client, admin, username="task-bob")
    owner = await resolve_user_id(client, admin, "task-alice")
    receipt = await registered_task(client, server, alice, owner)
    for auth in (bob, admin):
        missing = await client.get(f"{PREFIX}/tasks/{uuid4()}", headers=auth)
        denied = await client.get(f"{PREFIX}/tasks/{receipt.taskID}", headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert denied.headers["cache-control"] == "no-store"
        receipt_query = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert receipt_query.status_code == 404


async def test_invalid_task_uuid_has_safe_scoped_error_and_legacy_error_unchanged(env) -> None:
    client, _server, auth = env
    response = await client.get(f"{PREFIX}/tasks/private-task-input-marker", headers=auth)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert "private-task-input-marker" not in response.text
    legacy = await client.post("/api/auth/login", json={"username": "admin"})
    assert legacy.status_code == 422 and "detail" in legacy.json()


async def test_task_query_requires_header_authentication(env) -> None:
    client, server, auth = env
    endpoint = f"{PREFIX}/tasks/{uuid4()}"
    token = auth["Authorization"].split(" ", 1)[1]
    for headers, params in (
        ({}, {}),
        ({}, {"access_token": token}),
        (auth, {"access_token": token}),
    ):
        response = await client.get(endpoint, headers=headers, params=params)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authenticationRequired"
        assert response.headers["cache-control"] == "no-store"
    assert not server.paths.agentcomputer_identity.exists()
    assert (await client.get("/api/auth/me", params={"access_token": token})).status_code == 200


@pytest.mark.parametrize("failure", [StorageDataError, StorageUnavailableError, OSError])
async def test_task_storage_errors_do_not_expose_internal_data(env, monkeypatch, failure) -> None:
    client, _server, auth = env

    def unavailable(*_args):
        raise failure("private-task-storage-marker")

    monkeypatch.setattr(agentcomputer, "get_task", unavailable)
    response = await client.get(f"{PREFIX}/tasks/{uuid4()}", headers=auth)
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "storageUnavailable"
    if failure is StorageDataError:
        assert error == {
            "code": "storageUnavailable",
            "retryable": False,
            "reasonCode": "taskInvalid",
        }
    else:
        assert error == {"code": "storageUnavailable", "retryable": True}
    assert "private-task-storage-marker" not in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_ready_task_with_missing_thread_is_rejected_without_repair(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    receipt = await registered_task(client, server, auth, owner)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tasks SET octop_thread_id=NULL WHERE task_id=?", (str(receipt.taskID),)
        )
    response = await client.get(f"{PREFIX}/tasks/{receipt.taskID}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "taskInvalid"}
    }
    with server.services.db.connect() as conn:
        assert (
            conn.execute(
                "SELECT octop_thread_id FROM ac_tasks WHERE task_id=?", (str(receipt.taskID),)
            ).fetchone()[0]
            is None
        )


async def test_task_http_creation_remains_absent(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    receipt = await registered_task(client, server, auth, owner)
    for endpoint in (
        f"{PREFIX}/tasks",
        f"{PREFIX}/projects/{receipt.projectID}/tasks",
    ):
        response = await client.post(endpoint, headers=auth, json={})
        assert response.status_code in (404, 405)
        assert response.json()["error"]["code"] in ("resourceNotFound", "invalidRequest")
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0


async def test_openapi_documents_read_only_task_and_three_receipt_shapes(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        path = schema["paths"][f"{PREFIX}/tasks/{{taskID}}"]
        assert set(path) == {"get"}
        operation = path["get"]
        assert operation["tags"] == ["agentcomputer"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert "executionReady=false" in operation["description"]
        parameter = next(p for p in operation["parameters"] if p["in"] == "path")
        assert parameter["name"] == "taskID" and parameter["schema"]["format"] == "uuid"
        response = operation["responses"]["200"]["content"]["application/json"]
        assert response["schema"]["$ref"].endswith("/TaskView")
        assert TaskView.model_validate(response["example"]).executionReady is False
        for status in ("401", "403", "404", "422", "503"):
            error = operation["responses"][status]["content"]["application/json"]["schema"]
            assert error["$ref"].endswith("/ProtocolErrorEnvelope")
        receipts = schema["paths"][f"{PREFIX}/commands/{{commandID}}"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        assert {item["$ref"].rsplit("/", 1)[1] for item in receipts["anyOf"]} == {
            "CommandReceipt",
            "ProjectCreateReceipt",
            "TaskCreateReceipt",
        }
        assert (await client.get("/api/docs")).status_code == 200
