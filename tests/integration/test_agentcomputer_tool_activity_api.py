"""Owned Run activity never exposes a private effect or another user's Run."""

from __future__ import annotations

from uuid import uuid4

from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand
from tests.support.agentcomputer import register_ready_task
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


async def test_empty_tool_activity_is_owned_and_typed(env) -> None:
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
            prompt="Private tool prompt",
            permissionMode="askForApproval",
        ),
    )
    path = f"{PREFIX}/runs/{receipt.runID}/tool-activity"
    response = await client.get(path, headers=admin)
    assert response.status_code == 200
    assert response.json() == {"runID": str(receipt.runID), "items": []}
    assert response.headers["cache-control"] == "no-store"
    assert "Private tool prompt" not in response.text

    other = await create_user(client, admin, username="tool-activity-other")
    unknown = await client.get(f"{PREFIX}/runs/{uuid4()}/tool-activity", headers=other)
    denied = await client.get(path, headers=other)
    assert unknown.status_code == denied.status_code == 404
    assert unknown.json() == denied.json()
    assert (await client.get(path)).status_code == 401
    assert (
        await client.get(f"{PREFIX}/runs/not-a-uuid/tool-activity", headers=admin)
    ).status_code == 422

    operation = client._octop_app.openapi()["paths"][f"{PREFIX}/runs/{{runID}}/tool-activity"][
        "get"
    ]
    assert operation["summary"] == "Read an owned Run's Project document tool activity"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/RunToolActivity"
    )
    schema = client._octop_app.openapi()["components"]["schemas"]["ToolActivityItem"]
    assert set(schema["properties"]) == {"toolName", "path", "status", "outcome"}
