"""Public Task admission and Run result reads preserve the execution gate."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID, uuid4

from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import (
    auth_header,
    bootstrap_admin,
    create_user,
    resolve_user_id,
)

PREFIX = "/api/agentcomputer/v1"


async def test_task_create_is_closed_but_original_receipt_replays(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    original = await register_ready_task(client, server, auth, owner_id)
    assert server.services is not None
    stored = server.services.ac_command_repo.get(str(original.commandID), owner_id)
    assert stored is not None
    body = {
        "commandID": str(original.commandID),
        "idempotencyKey": stored.idempotency_key,
        "expectedRestoreEpoch": original.restoreEpoch,
        "expectedProjectRevision": 1,
        "projectID": str(original.projectID),
        "taskID": str(original.taskID),
        "title": "  Fixture task 中文  ",
    }
    path = f"{PREFIX}/projects/{original.projectID}/tasks"
    replay = await client.post(path, headers=auth, json=body)
    assert replay.status_code == 201
    assert replay.json() == {**original.model_dump(mode="json"), "replayed": True}
    assert replay.headers["cache-control"] == "no-store"

    new_body = {
        **body,
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "taskID": str(uuid4()),
    }
    closed = await client.post(path, headers=auth, json=new_body)
    assert closed.status_code == 503
    assert closed.json() == {
        "error": {
            "code": "runtimeUnavailable",
            "retryable": False,
            "reasonCode": "executionUnavailable",
        }
    }
    assert server.services.ac_task_repo.get(new_body["taskID"], owner_id) is None

    mismatch = await client.post(f"{PREFIX}/projects/{uuid4()}/tasks", headers=auth, json=new_body)
    assert mismatch.status_code == 422
    assert mismatch.json() == {"error": {"code": "invalidRequest", "retryable": False}}


async def test_task_create_uses_owned_project_and_fixed_command(env, monkeypatch) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    original = await register_ready_task(client, server, admin, owner_id)
    assert server.services is not None
    from octop.infra.agentcomputer import tasks

    monkeypatch.setattr(tasks, "text_task_available", lambda _services, *, model_ref: True)
    body = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "expectedProjectRevision": 1,
        "projectID": str(original.projectID),
        "taskID": str(uuid4()),
        "title": "盒子文本任务",
    }
    path = f"{PREFIX}/projects/{original.projectID}/tasks"
    created = await client.post(path, headers=admin, json=body)
    assert created.status_code == 201
    assert created.json()["taskID"] == body["taskID"]
    assert created.json()["executionReady"] is True
    view = await client.get(f"{PREFIX}/tasks/{body['taskID']}", headers=admin)
    assert view.status_code == 200 and view.json()["executionReady"] is True
    assert (await client.post(path, headers=admin, json=body)).json()["replayed"] is True

    other = await create_user(client, admin, username="task-result-other")
    denied = await client.post(
        path,
        headers=other,
        json={**body, "commandID": str(uuid4()), "idempotencyKey": str(uuid4())},
    )
    assert denied.status_code == 404
    assert denied.json() == {"error": {"code": "resourceNotFound", "retryable": False}}


async def test_task_create_uses_live_text_gate_and_replays_original_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "control"
    write_octop_config(
        home,
        agentcomputer_text_enabled=True,
        agentcomputer_text_model_ref="Trusted/glm-5",
        history_v2_enabled=True,
    )
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        assert server.services is not None
        provider_id = server.services.provider_repo.create(
            name="Trusted",
            kind="openai",
            base_url="https://provider.invalid/v1",
            api_key="fixture-key",
            models_json=json.dumps([{"id": "glm-5", "enabled": True}]),
        )
        project_id = uuid4()
        project = await client.post(
            f"{PREFIX}/projects",
            headers=auth,
            json={
                "commandID": str(uuid4()),
                "idempotencyKey": str(uuid4()),
                "expectedRestoreEpoch": 1,
                "projectID": str(project_id),
                "name": "GLM 文本项目",
                "configuration": {
                    "schemaVersion": 1,
                    "modelRef": "Trusted/glm-5",
                    "instructions": "使用中文回答",
                },
            },
        )
        assert project.status_code == 201
        profile = await client.get(f"{PREFIX}/capabilities", headers=auth)
        assert profile.json()["capabilities"]["tasks"]["available"] is True

        body = {
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "expectedProjectRevision": 1,
            "projectID": str(project_id),
            "taskID": str(uuid4()),
            "title": "纯文本任务",
        }
        path = f"{PREFIX}/projects/{project_id}/tasks"
        created = await client.post(path, headers=auth, json=body)
        assert created.status_code == 201
        assert created.json()["executionReady"] is True
        task_view = await client.get(f"{PREFIX}/tasks/{body['taskID']}", headers=auth)
        assert task_view.json()["executionReady"] is True

        owner_id = await resolve_user_id(client, auth, "admin")
        worker = Mock()
        with monkeypatch.context() as patch:
            patch.setattr(server, "agentcomputer_text_worker", worker, raising=False)
            turn = await client.post(
                f"{PREFIX}/tasks/{body['taskID']}/turns",
                headers=auth,
                json={
                    "commandID": str(uuid4()),
                    "idempotencyKey": str(uuid4()),
                    "expectedRestoreEpoch": 1,
                    "contextMode": "newMessage",
                    "userMessageID": str(uuid4()),
                    "expectedProjectRevision": 1,
                    "prompt": "用一句话回答",
                    "permissionMode": "askForApproval",
                },
            )
        assert turn.status_code == 201
        worker.schedule.assert_called_once_with(
            owner_id=owner_id, run_id=UUID(turn.json()["runID"])
        )

        server.services.provider_repo.update(provider_id, enabled=False)
        unavailable = await client.get(f"{PREFIX}/tasks/{body['taskID']}", headers=auth)
        assert unavailable.json()["executionReady"] is False
        replay = await client.post(path, headers=auth, json=body)
        assert replay.status_code == 201
        assert replay.json()["executionReady"] is True
        assert replay.json()["replayed"] is True
        closed = await client.post(
            path,
            headers=auth,
            json={
                **body,
                "commandID": str(uuid4()),
                "idempotencyKey": str(uuid4()),
                "taskID": str(uuid4()),
            },
        )
        assert closed.status_code == 503
        assert closed.json()["error"]["reasonCode"] == "executionUnavailable"


async def test_run_result_is_owned_and_has_no_unfinished_answer(env) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    assert server.services is not None
    receipt = admit_new_message(
        server.services,
        owner_id=owner_id,
        task_id=task.taskID,
        command=NewMessageCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            contextMode="newMessage",
            userMessageID=uuid4(),
            expectedProjectRevision=1,
            prompt="Only a queued fixture",
            permissionMode="askForApproval",
        ),
    )
    path = f"{PREFIX}/runs/{receipt.runID}/result"
    response = await client.get(path, headers=admin)
    assert response.status_code == 200
    assert response.json() == {
        "runID": str(receipt.runID),
        "state": "queued",
        "assistantMessageID": None,
        "text": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert "Only a queued fixture" not in response.text
    other = await create_user(client, admin, username="run-result-other")
    unknown = await client.get(f"{PREFIX}/runs/{uuid4()}/result", headers=other)
    denied = await client.get(path, headers=other)
    assert unknown.status_code == denied.status_code == 404
    assert unknown.json() == denied.json()
