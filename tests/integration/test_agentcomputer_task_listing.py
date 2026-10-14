"""Owned Task discovery projects complete Task and latest Run facts."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from octop.infra.agentcomputer import submission
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.models import (
    CommandReceipt,
    RunView,
    TaskCreateCommand,
    TaskListResponse,
)
from octop.infra.agentcomputer.tasks import register_task
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


async def _extra_task(server, owner_id: int, project_id: UUID, task_id: UUID) -> None:
    assert server.services is not None
    receipt = await asyncio.to_thread(
        register_task,
        server.services,
        owner_id=owner_id,
        command=TaskCreateCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            expectedProjectRevision=1,
            projectID=project_id,
            taskID=task_id,
            title=f"Task {task_id}",
        ),
    )
    assert receipt.taskID == task_id


def _change_cursor(cursor: str, **changes: object) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = json.loads(raw)
    payload.update(changes)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(canonical).rstrip(b"=").decode()


async def test_task_list_orders_by_creation_and_id_and_scopes_project(env) -> None:
    client, server, admin = env
    owner = await resolve_user_id(client, admin, "admin")
    first = await register_ready_task(client, server, admin, owner)
    ids = [UUID(int=number) for number in (1, 2, 3)]
    for task_id in ids:
        await _extra_task(server, owner, first.projectID, task_id)
    other_project = await register_ready_task(client, server, admin, owner)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
            (1_700_000_000_000, 1_700_000_000_000, str(first.taskID)),
        )
        for task_id in ids:
            conn.execute(
                "UPDATE ac_tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
                (1_700_000_000_001, 1_700_000_000_001, str(task_id)),
            )

    endpoint = f"{PREFIX}/projects/{first.projectID}/tasks"
    first_page = await client.get(endpoint, headers=admin, params={"limit": 2})
    assert first_page.status_code == 200
    assert first_page.headers["cache-control"] == "no-store"
    page = TaskListResponse.model_validate(first_page.json())
    assert [item.task.taskID for item in page.items] == [ids[2], ids[1]]
    assert all(item.latestRun is None for item in page.items)
    assert page.nextCursor is not None
    assert not {"octopThreadID", "agentID", "serverRoot"} & first_page.json()["items"][0].keys()
    assert str(server.paths.root) not in first_page.text

    second_page = await client.get(
        endpoint, headers=admin, params={"limit": 2, "cursor": page.nextCursor}
    )
    assert second_page.status_code == 200
    last = TaskListResponse.model_validate(second_page.json())
    assert [item.task.taskID for item in last.items] == [ids[0], first.taskID]
    assert last.nextCursor is None
    assert other_project.taskID not in [item.task.taskID for item in page.items + last.items]


async def test_task_list_owner_and_empty_project_are_distinct(env) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="list-task-alice")
    bob = await create_user(client, admin, username="list-task-bob")
    owner = await resolve_user_id(client, admin, "list-task-alice")
    task = await register_ready_task(client, server, alice, owner)
    endpoint = f"{PREFIX}/projects/{task.projectID}/tasks"
    assert (await client.get(endpoint, headers=alice)).json()["items"][0]["task"]["taskID"] == str(
        task.taskID
    )
    for headers in (bob, admin):
        hidden = await client.get(endpoint, headers=headers)
        missing = await client.get(f"{PREFIX}/projects/{uuid4()}/tasks", headers=headers)
        assert hidden.status_code == missing.status_code == 404
        assert (
            hidden.json()
            == missing.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert (
            await client.get(endpoint, headers=headers, params={"cursor": "not-a-cursor"})
        ).status_code == 404
    project = await client.post(
        f"{PREFIX}/projects",
        headers=alice,
        json={
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "projectID": str(uuid4()),
            "name": "Empty project",
            "configuration": {
                "schemaVersion": 1,
                "modelRef": "fixture/no-model",
                "instructions": "fixture",
            },
        },
    )
    assert project.status_code == 201
    empty = await client.get(
        f"{PREFIX}/projects/{project.json()['projectID']}/tasks", headers=alice
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "nextCursor": None}


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"cursor": "not-a-cursor"}])
async def test_task_list_invalid_page_query_is_scoped(env, query) -> None:
    client, server, admin = env
    owner = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner)
    response = await client.get(
        f"{PREFIX}/projects/{task.projectID}/tasks", headers=admin, params=query
    )
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}


async def test_task_list_cursor_is_canonical_and_bound_to_scope(env) -> None:
    client, server, admin = env
    owner = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner)
    await _extra_task(server, owner, task.projectID, uuid4())
    other = await register_ready_task(client, server, admin, owner)
    endpoint = f"{PREFIX}/projects/{task.projectID}/tasks"
    first = await client.get(endpoint, headers=admin, params={"limit": 1})
    cursor = first.json()["nextCursor"]
    assert type(cursor) is str
    for invalid in (
        cursor + "=",
        _change_cursor(cursor, ownerID=owner + 1),
        _change_cursor(cursor, projectID=str(other.projectID)),
        _change_cursor(cursor, boxID=str(uuid4())),
        _change_cursor(cursor, restoreEpoch=2),
        _change_cursor(cursor, createdAt=True),
        _change_cursor(cursor, taskID=str(uuid4()).upper()),
        _change_cursor(cursor, extra="unknown"),
    ):
        response = await client.get(endpoint, headers=admin, params={"cursor": invalid})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalidRequest"
    wrong_project = await client.get(
        f"{PREFIX}/projects/{other.projectID}/tasks", headers=admin, params={"cursor": cursor}
    )
    assert wrong_project.status_code == 422
    identity_path = server.paths.agentcomputer_identity
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] += 1
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    stale = await client.get(endpoint, headers=admin, params={"cursor": cursor})
    assert stale.status_code == 422


async def test_task_list_latest_run_matches_full_run_read_and_bad_fact_fails_closed(
    env, monkeypatch
) -> None:
    client, server, admin = env
    owner = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner)
    assert server.services is not None
    profile = get_capabilities(server.services)
    profile = profile.model_copy(
        update={
            "supportedPermissionModes": ["askForApproval"],
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
        }
    )
    monkeypatch.setattr(submission, "get_capabilities", lambda _services: profile)
    monkeypatch.setattr(submission, "text_task_available", lambda _services, *, model_ref: True)
    body = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "contextMode": "newMessage",
        "userMessageID": str(uuid4()),
        "expectedProjectRevision": 1,
        "prompt": "fixture text",
        "inputArtifactIDs": [],
        "selectedSkillIDs": [],
        "permissionMode": "askForApproval",
    }
    turn = await client.post(f"{PREFIX}/tasks/{task.taskID}/turns", headers=admin, json=body)
    assert turn.status_code == 201
    receipt = CommandReceipt.model_validate(turn.json())
    run = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=admin)
    assert run.status_code == 200
    listed = await client.get(f"{PREFIX}/projects/{task.projectID}/tasks", headers=admin)
    assert listed.status_code == 200
    item = TaskListResponse.model_validate(listed.json()).items[0]
    assert item.task.taskID == task.taskID
    assert item.latestRun == RunView.model_validate(run.json())
    assert "fixture text" not in listed.text

    cancelled = await client.post(
        f"{PREFIX}/runs/{receipt.runID}/cancel",
        headers=admin,
        json={
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "expectedRevision": 1,
        },
    )
    assert cancelled.status_code == 201
    second = await client.post(
        f"{PREFIX}/tasks/{task.taskID}/turns",
        headers=admin,
        json={
            **body,
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "userMessageID": str(uuid4()),
            "prompt": "second fixture text",
        },
    )
    assert second.status_code == 201
    receipt = CommandReceipt.model_validate(second.json())
    newest = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=admin)
    assert newest.status_code == 200
    listed = await client.get(f"{PREFIX}/projects/{task.projectID}/tasks", headers=admin)
    assert TaskListResponse.model_validate(listed.json()).items[
        0
    ].latestRun == RunView.model_validate(newest.json())

    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_events SET payload_json=? WHERE run_id=? AND event_type='run.accepted'",
            ('{"private":"damaged"}', str(receipt.runID)),
        )
    damaged = await client.get(f"{PREFIX}/projects/{task.projectID}/tasks", headers=admin)
    assert damaged.status_code == 503
    assert damaged.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "taskInvalid"}
    }
    assert "private" not in damaged.text


async def test_task_list_project_and_task_corruption_fail_closed(env) -> None:
    client, server, admin = env
    owner = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_tasks SET octop_thread_id=NULL WHERE task_id=?", (str(task.taskID),)
        )
    endpoint = f"{PREFIX}/projects/{task.projectID}/tasks"
    broken = await client.get(endpoint, headers=admin)
    assert broken.status_code == 503
    assert broken.json()["error"]["reasonCode"] == "taskInvalid"
    with server.services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_tasks WHERE task_id=?", (str(task.taskID),))
        conn.execute(
            "UPDATE ac_project_preparations SET intent_digest=? WHERE project_id=?",
            ("private-damaged-intent", str(task.projectID)),
        )
    empty_broken = await client.get(endpoint, headers=admin)
    assert empty_broken.status_code == 503
    assert empty_broken.json()["error"]["reasonCode"] == "taskInvalid"


async def test_task_list_openapi_has_typed_items_and_cursor(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        route = schema["paths"][f"{PREFIX}/projects/{{projectID}}/tasks"]
        assert set(route) == {"get", "post"}
        listing = route["get"]
        assert listing["tags"] == ["agentcomputer"]
        assert listing["security"] == [{"BearerAuth": []}]
        parameters = {parameter["name"]: parameter["schema"] for parameter in listing["parameters"]}
        assert parameters["limit"]["default"] == 50
        assert parameters["limit"]["minimum"] == 1
        assert parameters["limit"]["maximum"] == 100
        assert {"type": "string"} in parameters["cursor"]["anyOf"]
        response = listing["responses"]["200"]["content"]["application/json"]
        assert response["schema"]["$ref"].endswith("/TaskListResponse")
        for status in ("401", "403", "404", "422", "503"):
            error = listing["responses"][status]["content"]["application/json"]["schema"]
            assert error["$ref"].endswith("/ProtocolErrorEnvelope")
        assert (await client.get("/api/docs")).status_code == 200
