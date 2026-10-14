"""Owned Task timelines expose original prompts and verified Run results."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand
from octop.infra.agentcomputer.turn_listing import TurnListResponse
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def _accept(server, owner_id: int, task_id: UUID, prompt: str):
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
            prompt=prompt,
            permissionMode="askForApproval",
        ),
    )


def _change_cursor(cursor: str, **changes: object) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = json.loads(raw)
    payload.update(changes)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


async def _two_turns(env):
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    first = _accept(server, owner_id, task.taskID, "第一轮原文")
    stopped = await client.post(
        f"{PREFIX}/runs/{first.runID}/cancel",
        headers=admin,
        json={
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "expectedRevision": 1,
        },
    )
    assert stopped.status_code == 201
    second = _accept(server, owner_id, task.taskID, "第二轮原文")
    return task, first, second


async def test_turn_list_reads_two_original_messages_and_paginates(env) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/turns"
    empty = await client.get(endpoint, headers=admin)
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "nextCursor": None}

    task, first, second = await _two_turns(env)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/turns"
    newest = await client.get(endpoint, headers=admin, params={"limit": 1})
    assert newest.status_code == 200
    assert newest.headers["cache-control"] == "no-store"
    page = TurnListResponse.model_validate(newest.json())
    assert len(page.items) == 1 and page.nextCursor is not None
    assert page.items[0].turnID == second.turnID
    assert page.items[0].userMessageID == second.userMessageID
    assert page.items[0].prompt == "第二轮原文"
    assert page.items[0].run.runID == second.runID
    assert page.items[0].run.state == page.items[0].result.state == "queued"
    assert page.items[0].result.text is None
    older = await client.get(
        endpoint, headers=admin, params={"limit": 1, "cursor": page.nextCursor}
    )
    assert older.status_code == 200
    old_page = TurnListResponse.model_validate(older.json())
    assert len(old_page.items) == 1 and old_page.nextCursor is None
    assert old_page.items[0].turnID == first.turnID
    assert old_page.items[0].prompt == "第一轮原文"
    assert old_page.items[0].run.state == old_page.items[0].result.state == "stopped"
    assert old_page.items[0].result.text is None
    assert not {"octopThreadID", "agentID", "serverRoot"} & newest.json()["items"][0].keys()
    assert str(server.paths.root) not in newest.text


async def test_turn_list_hides_other_users_tasks_before_cursor_validation(env) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="turn-list-alice")
    bob = await create_user(client, admin, username="turn-list-bob")
    owner_id = await resolve_user_id(client, admin, "turn-list-alice")
    task = await register_ready_task(client, server, alice, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/turns"
    for headers in (admin, bob):
        hidden = await client.get(endpoint, headers=headers, params={"cursor": "invalid"})
        missing = await client.get(
            f"{PREFIX}/tasks/{uuid4()}/turns", headers=headers, params={"cursor": "invalid"}
        )
        assert hidden.status_code == missing.status_code == 404
        assert (
            hidden.json()
            == missing.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"cursor": "invalid"}])
async def test_turn_list_rejects_invalid_page_query(env, query) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    response = await client.get(f"{PREFIX}/tasks/{task.taskID}/turns", headers=admin, params=query)
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}


async def test_turn_list_cursor_scope_and_old_epoch_history(env) -> None:
    client, server, admin = env
    task, first, _second = await _two_turns(env)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/turns"
    first_page = await client.get(endpoint, headers=admin, params={"limit": 1})
    cursor = first_page.json()["nextCursor"]
    assert type(cursor) is str
    for invalid in (
        cursor + "=",
        _change_cursor(cursor, ownerID=123456),
        _change_cursor(cursor, taskID=str(uuid4())),
        _change_cursor(cursor, boxID=str(uuid4())),
        _change_cursor(cursor, restoreEpoch=2),
        _change_cursor(cursor, afterID=True),
        _change_cursor(cursor, turnID=str(uuid4()).upper()),
        _change_cursor(cursor, extra="unknown"),
    ):
        response = await client.get(endpoint, headers=admin, params={"cursor": invalid})
        assert response.status_code == 422
    other = await register_ready_task(
        client, server, admin, await resolve_user_id(client, admin, "admin")
    )
    assert (
        await client.get(
            f"{PREFIX}/tasks/{other.taskID}/turns", headers=admin, params={"cursor": cursor}
        )
    ).status_code == 422

    identity_path = server.paths.agentcomputer_identity
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] = 2
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    assert (await client.get(endpoint, headers=admin, params={"cursor": cursor})).status_code == 422
    current = await client.get(endpoint, headers=admin)
    assert current.status_code == 200
    assert [item["prompt"] for item in current.json()["items"]] == [
        "第二轮原文",
        "第一轮原文",
    ]
    old_run = await client.get(f"{PREFIX}/runs/{first.runID}/result", headers=admin)
    assert old_run.status_code == 200 and old_run.json()["state"] == "stopped"


async def test_turn_list_corruption_fails_the_whole_page(env) -> None:
    client, server, admin = env
    task, first, _second = await _two_turns(env)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_events SET payload_json=? WHERE run_id=? AND event_type='run.accepted'",
            ('{"private":"damaged"}', str(first.runID)),
        )
    response = await client.get(f"{PREFIX}/tasks/{task.taskID}/turns", headers=admin)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "turnInvalid"}
    }
    assert "private" not in response.text and "第一轮原文" not in response.text


async def test_turn_list_openapi_is_typed_and_shared_with_post(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        route = schema["paths"][f"{PREFIX}/tasks/{{taskID}}/turns"]
        assert set(route) == {"get", "post"}
        listing = route["get"]
        assert listing["tags"] == ["agentcomputer"]
        assert listing["security"] == [{"BearerAuth": []}]
        parameters = {parameter["name"]: parameter["schema"] for parameter in listing["parameters"]}
        assert parameters["limit"]["default"] == 20
        assert parameters["limit"]["minimum"] == 1
        assert parameters["limit"]["maximum"] == 100
        response = listing["responses"]["200"]["content"]["application/json"]
        assert response["schema"] == {"$ref": "#/components/schemas/TurnListResponse"}
        item = schema["components"]["schemas"]["TurnListItem"]["properties"]
        assert item["run"] == {"$ref": "#/components/schemas/RunView"}
        assert item["result"]["$ref"] == "#/components/schemas/RunResult"
