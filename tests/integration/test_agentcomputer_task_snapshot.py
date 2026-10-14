"""Task control snapshots bridge a validated current view to event polling."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def _accept(server, owner_id, task_id):
    assert server.services is not None
    return admit_new_message(
        server.services,
        owner_id=owner_id,
        task_id=task_id,
        command=NewMessageCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            contextMode="newMessage",
            userMessageID=uuid4(),
            expectedProjectRevision=1,
            prompt="只回复一个词",
            permissionMode="askForApproval",
        ),
    )


async def test_task_snapshot_empty_and_event_continuation(env) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}"

    empty = await client.get(f"{endpoint}/snapshot", headers=admin)
    assert empty.status_code == 200
    assert empty.headers["cache-control"] == "no-store"
    baseline = empty.json()
    assert baseline["task"]["taskID"] == str(task.taskID)
    assert baseline["task"]["projectID"] == str(task.projectID)
    assert baseline["latestRun"] is None
    assert baseline["coveredSequence"] == 0
    assert isinstance(baseline["nextCursor"], str)
    receipt = _accept(server, owner_id, task.taskID)

    page = await client.get(
        f"{endpoint}/events", headers=admin, params={"cursor": baseline["nextCursor"]}
    )
    assert page.status_code == 200
    assert [(item["sequence"], item["runID"]) for item in page.json()["items"]] == [
        (1, str(receipt.runID))
    ]

    after = await client.get(f"{endpoint}/snapshot", headers=admin)
    assert after.status_code == 200
    state = after.json()
    assert state["coveredSequence"] == 1
    assert state["latestRun"]["runID"] == str(receipt.runID)
    assert state["latestRun"]["taskID"] == str(task.taskID)
    assert state["latestRun"]["projectID"] == str(task.projectID)
    caught_up = await client.get(
        f"{endpoint}/events", headers=admin, params={"cursor": state["nextCursor"]}
    )
    assert caught_up.status_code == 200
    assert caught_up.json()["items"] == []
    assert caught_up.json()["coveredSequence"] == 1

    stopped = await client.post(
        f"{PREFIX}/runs/{receipt.runID}/cancel",
        headers=admin,
        json={
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "expectedRevision": 1,
        },
    )
    assert stopped.status_code == 201
    next_page = await client.get(
        f"{endpoint}/events", headers=admin, params={"cursor": state["nextCursor"]}
    )
    assert [(item["sequence"], item["runID"]) for item in next_page.json()["items"]] == [
        (2, str(receipt.runID)),
        (3, str(receipt.runID)),
    ]
    terminal = await client.get(f"{endpoint}/snapshot", headers=admin)
    assert terminal.status_code == 200
    assert terminal.json()["coveredSequence"] == 3
    assert terminal.json()["latestRun"]["state"] == "stopped"

    second = _accept(server, owner_id, task.taskID)
    next_turn = await client.get(f"{endpoint}/snapshot", headers=admin)
    assert next_turn.status_code == 200
    assert next_turn.json()["coveredSequence"] == 4
    assert next_turn.json()["latestRun"]["runID"] == str(second.runID)
    assert next_turn.json()["latestRun"]["state"] == "queued"
    continuation = await client.get(
        f"{endpoint}/events", headers=admin, params={"cursor": terminal.json()["nextCursor"]}
    )
    assert [(item["sequence"], item["runID"]) for item in continuation.json()["items"]] == [
        (4, str(second.runID))
    ]


async def test_task_snapshot_owner_hiding_and_restore_epoch(env) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="snapshot-owner-alice")
    bob = await create_user(client, admin, username="snapshot-owner-bob")
    owner_id = await resolve_user_id(client, admin, "snapshot-owner-alice")
    task = await register_ready_task(client, server, alice, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/snapshot"
    for headers in (admin, bob):
        hidden = await client.get(endpoint, headers=headers)
        missing = await client.get(f"{PREFIX}/tasks/{uuid4()}/snapshot", headers=headers)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()

    first = await client.get(endpoint, headers=alice)
    assert first.status_code == 200
    old_cursor = first.json()["nextCursor"]
    identity_path = server.paths.agentcomputer_identity
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] = 2
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    assert (
        await client.get(
            f"{PREFIX}/tasks/{task.taskID}/events", headers=alice, params={"cursor": old_cursor}
        )
    ).status_code == 422
    refreshed = await client.get(endpoint, headers=alice)
    assert refreshed.status_code == 200
    assert refreshed.json()["coveredSequence"] == 0
    assert refreshed.json()["nextCursor"] != old_cursor
    assert (
        await client.get(
            f"{PREFIX}/tasks/{task.taskID}/events",
            headers=alice,
            params={"cursor": refreshed.json()["nextCursor"]},
        )
    ).status_code == 200


@pytest.mark.parametrize("damage", ["gap", "watermark", "anchor", "runReceipt"])
async def test_task_snapshot_rejects_corrupted_control_graph(env, damage: str) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    receipt = _accept(server, owner_id, task.taskID)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        if damage == "gap":
            conn.execute("DELETE FROM ac_events WHERE run_id=?", (str(receipt.runID),))
        elif damage == "watermark":
            conn.execute(
                "UPDATE ac_tasks SET next_sequence=next_sequence+1 WHERE task_id=?",
                (str(task.taskID),),
            )
        elif damage == "anchor":
            conn.execute(
                "UPDATE ac_events SET payload_json='{}' WHERE run_id=?", (str(receipt.runID),)
            )
        else:
            conn.execute(
                "UPDATE ac_commands SET receipt_json='{}' WHERE command_id=?",
                (str(receipt.commandID),),
            )
    response = await client.get(f"{PREFIX}/tasks/{task.taskID}/snapshot", headers=admin)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storageUnavailable"
    assert "payload_json" not in response.text


async def test_task_snapshot_openapi_is_typed(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        route = schema["paths"][f"{PREFIX}/tasks/{{taskID}}/snapshot"]["get"]
        assert route["tags"] == ["agentcomputer"]
        assert route["security"] == [{"BearerAuth": []}]
        response = route["responses"]["200"]["content"]["application/json"]
        assert response["schema"] == {"$ref": "#/components/schemas/TaskControlSnapshot"}
        fields = schema["components"]["schemas"]["TaskControlSnapshot"]["properties"]
        assert set(fields) == {"task", "latestRun", "coveredSequence", "nextCursor"}
