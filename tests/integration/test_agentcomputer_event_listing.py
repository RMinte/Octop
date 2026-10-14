"""AgentComputer Task events expose durable, scoped control facts only."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def _accept(server, owner_id: int, task_id: UUID):
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


def _change_cursor(cursor: str, **changes: object) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = json.loads(raw)
    payload.update(changes)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()


async def test_task_events_page_poll_and_safe_projection(env) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/events"
    empty = await client.get(endpoint, headers=admin)
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["coveredSequence"] == 0
    assert empty.json()["hasMore"] is False
    cursor = empty.json()["nextCursor"]

    receipt = _accept(server, owner_id, task.taskID)
    page = await client.get(endpoint, headers=admin, params={"cursor": cursor, "limit": 1})
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert page.json()["coveredSequence"] == 1
    assert page.json()["hasMore"] is False
    assert page.json()["items"] == [
        {
            "eventID": page.json()["items"][0]["eventID"],
            "taskID": str(task.taskID),
            "turnID": str(receipt.turnID),
            "runID": str(receipt.runID),
            "sequence": 1,
            "occurredAt": page.json()["items"][0]["occurredAt"],
            "type": "run.accepted",
        }
    ]
    assert "payload" not in page.text
    assert "configDigest" not in page.text
    assert str(server.paths.root) not in page.text
    assert (
        await client.get(endpoint, headers=admin, params={"cursor": page.json()["nextCursor"]})
    ).json()["items"] == []

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
    first = await client.get(
        endpoint, headers=admin, params={"cursor": page.json()["nextCursor"], "limit": 1}
    )
    assert first.status_code == 200
    assert first.json()["coveredSequence"] == 3
    assert first.json()["hasMore"] is True
    assert [item["type"] for item in first.json()["items"]] == ["run.cancelRequested"]
    next_receipt = _accept(server, owner_id, task.taskID)
    second = await client.get(
        endpoint, headers=admin, params={"cursor": first.json()["nextCursor"], "limit": 1}
    )
    assert second.status_code == 200
    assert second.json()["coveredSequence"] == 3
    assert second.json()["hasMore"] is False
    assert [item["type"] for item in second.json()["items"]] == ["run.finished"]
    assert [item["sequence"] for item in first.json()["items"] + second.json()["items"]] == [2, 3]
    resumed = await client.get(
        endpoint, headers=admin, params={"cursor": second.json()["nextCursor"]}
    )
    assert resumed.status_code == 200
    assert resumed.json()["coveredSequence"] == 4
    assert resumed.json()["hasMore"] is False
    assert [(item["sequence"], item["runID"]) for item in resumed.json()["items"]] == [
        (4, str(next_receipt.runID))
    ]


async def test_task_events_owner_hiding_and_cursor_scope(env) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="event-owner-alice")
    bob = await create_user(client, admin, username="event-owner-bob")
    owner_id = await resolve_user_id(client, admin, "event-owner-alice")
    task = await register_ready_task(client, server, alice, owner_id)
    endpoint = f"{PREFIX}/tasks/{task.taskID}/events"
    _accept(server, owner_id, task.taskID)
    first = await client.get(endpoint, headers=alice)
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    for headers in (admin, bob):
        hidden = await client.get(endpoint, headers=headers, params={"cursor": "bad"})
        missing = await client.get(
            f"{PREFIX}/tasks/{uuid4()}/events", headers=headers, params={"cursor": "bad"}
        )
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()
    for invalid in (
        cursor + "=",
        _change_cursor(cursor, boxID=str(uuid4())),
        _change_cursor(cursor, ownerID=123456),
        _change_cursor(cursor, taskID=str(uuid4())),
        _change_cursor(cursor, afterSequence=True),
        _change_cursor(cursor, afterEventID=str(uuid4())),
        _change_cursor(cursor, throughSequence=0),
        _change_cursor(cursor, extra="bad"),
    ):
        response = await client.get(endpoint, headers=alice, params={"cursor": invalid})
        assert response.status_code == 422
    other = await register_ready_task(client, server, alice, owner_id)
    assert (
        await client.get(
            f"{PREFIX}/tasks/{other.taskID}/events", headers=alice, params={"cursor": cursor}
        )
    ).status_code == 422

    identity_path = server.paths.agentcomputer_identity
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["restoreEpoch"] = 2
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    assert (await client.get(endpoint, headers=alice, params={"cursor": cursor})).status_code == 422
    history = await client.get(endpoint, headers=alice)
    assert history.status_code == 200
    assert [item["type"] for item in history.json()["items"]] == ["run.accepted"]


async def test_task_events_follow_validated_dispatch_and_interruption(env, monkeypatch) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    receipt = _accept(server, owner_id, task.taskID)
    assert server.services is not None

    def available_profile(services):
        profile = get_capabilities(services)
        return profile.model_copy(
            update={
                "supportedPermissionModes": ["askForApproval"],
                "capabilities": profile.capabilities.model_copy(
                    update={"runs": CapabilityStatus(supported=True, available=True)}
                ),
            }
        )

    monkeypatch.setattr(dispatch, "get_capabilities", available_profile)
    monkeypatch.setattr(dispatch, "text_task_available", lambda _services, *, model_ref: True)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        server.services,
        owner_id=owner_id,
        run_id=receipt.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )

    async def enter(invocation):
        return await asyncio.to_thread(dispatch.accept_engine_entry, server.services, invocation)

    await dispatch.dispatch_run(
        server.services, claim=claim, invoke=enter, active_runs=server.agentcomputer_active_runs
    )
    current = dispatch.get_claim(server.services, owner_id=owner_id, run_id=receipt.runID)
    assert current is not None
    await asyncio.to_thread(dispatch.interrupt_uncertain_run, server.services, claim=current)
    page = await client.get(f"{PREFIX}/tasks/{task.taskID}/events", headers=admin)
    assert page.status_code == 200
    assert [event["type"] for event in page.json()["items"]] == [
        "run.accepted",
        "run.claimed",
        "run.dispatching",
        "run.engineAccepted",
        "run.interrupted",
    ]
    assert [event["sequence"] for event in page.json()["items"]] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"cursor": "bad"}])
async def test_task_events_rejects_invalid_query(env, query) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    response = await client.get(f"{PREFIX}/tasks/{task.taskID}/events", headers=admin, params=query)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalidRequest"


@pytest.mark.parametrize("damage", ["payload", "gap", "unknownType", "receipt"])
async def test_task_events_corruption_fails_whole_page(env, damage: str) -> None:
    client, server, admin = env
    owner_id = await resolve_user_id(client, admin, "admin")
    task = await register_ready_task(client, server, admin, owner_id)
    receipt = _accept(server, owner_id, task.taskID)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        if damage == "payload":
            conn.execute(
                "UPDATE ac_events SET payload_json='{}' WHERE run_id=?",
                (str(receipt.runID),),
            )
        elif damage == "gap":
            conn.execute("DELETE FROM ac_events WHERE run_id=?", (str(receipt.runID),))
        elif damage == "unknownType":
            conn.execute(
                "UPDATE ac_events SET event_type='future.finished' WHERE run_id=?",
                (str(receipt.runID),),
            )
        else:
            conn.execute(
                "UPDATE ac_commands SET receipt_json='{}' WHERE command_id=?",
                (str(receipt.commandID),),
            )
    response = await client.get(f"{PREFIX}/tasks/{task.taskID}/events", headers=admin)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storageUnavailable"
    assert "payload_json" not in response.text


async def test_task_events_openapi_is_typed(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        route = schema["paths"][f"{PREFIX}/tasks/{{taskID}}/events"]["get"]
        assert route["tags"] == ["agentcomputer"]
        assert route["security"] == [{"BearerAuth": []}]
        response = route["responses"]["200"]["content"]["application/json"]
        assert response["schema"] == {"$ref": "#/components/schemas/TaskEventListResponse"}
        fields = schema["components"]["schemas"]["TaskEventItem"]["properties"]
        assert set(fields) == {
            "eventID",
            "taskID",
            "turnID",
            "runID",
            "sequence",
            "occurredAt",
            "type",
        }
