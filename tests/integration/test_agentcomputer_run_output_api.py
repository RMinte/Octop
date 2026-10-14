"""Authenticated output snapshots do not expose another user's Run or prompt."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import FastAPI

from octop.api.deps import current_bearer_user, get_server
from octop.api.routers.agentcomputer_output import router as output_router
from octop.infra.agentcomputer.commands import admit_new_message, content_digest
from octop.infra.agentcomputer.http_operations import HttpOperationRegistry
from octop.infra.agentcomputer.models import NewMessageCommand
from octop.infra.users.identity import User
from tests.support.agentcomputer import register_ready_task
from tests.support.auth import create_user, resolve_user_id
from tests.unit.agentcomputer.test_checkpoint_evidence import persisted as persisted
from tests.unit.agentcomputer.test_engine_entry import entry as entry
from tests.unit.agentcomputer.test_submission_runs import control as control

PREFIX = "/api/agentcomputer/v1"


async def test_run_output_is_owned_and_empty_before_engine_entry(env) -> None:
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
            prompt="Private output prompt",
            permissionMode="askForApproval",
        ),
    )
    path = f"{PREFIX}/runs/{receipt.runID}/output"
    response = await client.get(path, headers=admin)
    assert response.status_code == 200
    assert response.json() == {
        "runID": str(receipt.runID),
        "state": "queued",
        "text": None,
        "digest": None,
        "provisional": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert "Private output prompt" not in response.text

    other = await create_user(client, admin, username="run-output-other")
    unknown = await client.get(f"{PREFIX}/runs/{uuid4()}/output", headers=other)
    denied = await client.get(path, headers=other)
    assert unknown.status_code == denied.status_code == 404
    assert unknown.json() == denied.json()
    malformed = await client.get(f"{PREFIX}/runs/not-a-uuid/output", headers=admin)
    assert malformed.status_code == 422
    unauthenticated = await client.get(path)
    assert unauthenticated.status_code == 401

    schema = client._octop_app.openapi()
    operation = schema["paths"][f"{PREFIX}/runs/{{runID}}/output"]["get"]
    assert operation["summary"] == "Read an owned Run's text output snapshot"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/RunOutputSnapshot"
    )
    model = schema["components"]["schemas"]["RunOutputSnapshot"]
    assert set(model["properties"]) == {"runID", "state", "text", "digest", "provisional"}


async def test_http_success_exposes_only_receipt_verified_final_text(persisted) -> None:
    fixture, _, _ = persisted
    registry = HttpOperationRegistry()
    server = SimpleNamespace(
        services=fixture.services, database_bound=True, agentcomputer_http_operations=registry
    )
    app = FastAPI()
    app.include_router(output_router, prefix=PREFIX)
    app.dependency_overrides[get_server] = lambda: server
    app.dependency_overrides[current_bearer_user] = lambda: User(1, "owner", "admin", None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"{PREFIX}/runs/{fixture.claim.intent.runID}/output")
            with fixture.history_archive.store.transaction() as conn:
                conn.execute(
                    "DELETE FROM documents WHERE id=?",
                    (f"message:{fixture.claim.intent.turnID}:1",),
                )
            damaged = await client.get(f"{PREFIX}/runs/{fixture.claim.intent.runID}/output")
        assert response.status_code == 200
        assert response.json() == {
            "runID": str(fixture.claim.intent.runID),
            "state": "succeeded",
            "text": "fixture response",
            "digest": content_digest("fixture response"),
            "provisional": False,
        }
        assert response.headers["cache-control"] == "no-store"
        assert damaged.status_code == 503
        assert damaged.json() == {
            "error": {
                "code": "storageUnavailable",
                "retryable": False,
                "reasonCode": "runInvalid",
            }
        }
    finally:
        await registry.drain()
