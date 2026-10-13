"""Run-scoped HTTP cancellation reports durable facts without stopping a thread."""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.cancellations import CancelRunReceipt
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.limits import MAX_COMMAND_BYTES
from octop.infra.agentcomputer.models import CommandReceipt, NewMessageCommand
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


def cancel_body(*, revision: int = 1, **changes: object) -> dict[str, object]:
    return {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "expectedRevision": revision,
        **changes,
    }


async def seed_run(client, server, auth, owner_id: int) -> CommandReceipt:
    """Register an isolated Run directly; production execution admission remains closed."""
    task = await register_ready_task(client, server, auth, owner_id)
    assert server.services is not None
    command = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt="Cancellation HTTP fixture",
        permissionMode="askForApproval",
    )
    return await asyncio.to_thread(
        admit_new_message,
        server.services,
        owner_id=owner_id,
        task_id=task.taskID,
        command=command,
    )


def journal_counts(server) -> tuple[int, int, int]:
    assert server.services is not None
    with server.services.db.connect() as conn:
        return tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ac_commands", "ac_run_cancellations", "ac_events")
        )


async def test_queued_cancel_stops_exact_run_and_preserves_original_receipts(env, monkeypatch):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    assert server.app_runtime is not None
    thread_cancel = Mock(side_effect=AssertionError("thread-wide cancel must not run"))
    monkeypatch.setattr(server.app_runtime.agent_registry, "cancel_stream", thread_cancel)
    body = cancel_body()

    response = await client.post(f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=body)

    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    receipt = CancelRunReceipt.model_validate(response.json())
    assert receipt.runID == accepted.runID and receipt.status == "stopped"
    assert receipt.commandID == UUID(str(body["commandID"])) and not receipt.replayed
    assert (receipt.requestRunRevision, receipt.resultRunRevision) == (2, 3)
    thread_cancel.assert_not_called()
    current = await client.get(f"{PREFIX}/runs/{accepted.runID}", headers=auth)
    assert current.status_code == 200
    assert current.json()["state"] == "stopped"
    assert current.json()["cancelRequested"] is True
    original = await client.get(f"{PREFIX}/commands/{accepted.commandID}", headers=auth)
    assert original.json() == accepted.model_dump(mode="json")
    queried = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert queried.status_code == 200 and queried.json() == receipt.model_dump(mode="json")
    assert server.services is not None
    task = server.services.ac_task_repo.get(str(accepted.taskID), owner_id)
    assert task is not None and task.active_run_id is None
    project = server.services.ac_project_repo.get(task.project_id, owner_id)
    assert project is not None and project.active_run_id is None


async def test_same_key_retry_returns_original_cancel_receipt_without_new_facts(env):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    body = cancel_body()
    first = await client.post(f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=body)
    assert first.status_code == 201
    before = journal_counts(server)
    retry = {**body, "commandID": str(uuid4())}
    second = await client.post(f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=retry)
    assert second.status_code == 201
    assert second.json() == {**first.json(), "replayed": True}
    assert journal_counts(server) == before
    assert (
        await client.get(f"{PREFIX}/commands/{retry['commandID']}", headers=auth)
    ).status_code == 404
    conflict = await client.post(
        f"{PREFIX}/runs/{accepted.runID}/cancel",
        headers=auth,
        json={**body, "expectedRevision": 2},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotencyConflict"
    assert journal_counts(server) == before


async def test_cancel_is_owner_scoped_even_for_admin(env):
    client, server, admin = env
    alice = await create_user(client, admin, username="cancel-alice")
    bob = await create_user(client, admin, username="cancel-bob")
    alice_id = await resolve_user_id(client, admin, "cancel-alice")
    accepted = await seed_run(client, server, alice, alice_id)
    before = journal_counts(server)
    for auth in (bob, admin):
        for run_id in (accepted.runID, uuid4()):
            response = await client.post(
                f"{PREFIX}/runs/{run_id}/cancel", headers=auth, json=cancel_body()
            )
            assert response.status_code == 404
            assert response.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
    assert journal_counts(server) == before


async def test_cancel_requires_header_bearer_and_rejects_untrusted_identifiers(env):
    client, server, auth = env
    token = auth["Authorization"].split(" ", 1)[1]
    path = f"{PREFIX}/runs/{uuid4()}/cancel"
    for headers, params in (
        ({}, {}),
        ({}, {"access_token": token}),
        (auth, {"access_token": token}),
    ):
        response = await client.post(path, headers=headers, params=params, json=cancel_body())
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
        assert response.headers["cache-control"] == "no-store"
    for path, body in (
        (f"{PREFIX}/runs/private-input-marker/cancel", cancel_body()),
        (f"{PREFIX}/runs/{uuid4()}/cancel", cancel_body(expectedRevision="1")),
        (f"{PREFIX}/runs/{uuid4()}/cancel", cancel_body(ownerID="private-input-marker")),
    ):
        response = await client.post(path, headers=auth, json=body)
        assert response.status_code == 422
        assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
        assert "private-input-marker" not in response.text
    assert journal_counts(server) == (0, 0, 0)


async def test_stale_revision_or_restore_epoch_does_not_record_cancel(env):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    before = journal_counts(server)
    for body, code in (
        (cancel_body(revision=2), "revisionConflict"),
        (cancel_body(expectedRestoreEpoch=2), "restoreEpochConflict"),
    ):
        response = await client.post(
            f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=body
        )
        assert response.status_code == 409 and response.json()["error"]["code"] == code
    assert journal_counts(server) == before
    current = await client.get(f"{PREFIX}/runs/{accepted.runID}", headers=auth)
    assert current.json()["state"] == "queued"
    assert current.json()["cancelRequested"] is False


async def test_claimed_cancel_only_records_request_and_retains_both_lanes(env, monkeypatch):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    assert server.services is not None and server.app_runtime is not None
    profile = get_capabilities(server.services)
    worker_profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(dispatch, "get_capabilities", lambda _: worker_profile)
    await asyncio.to_thread(
        dispatch.claim_run,
        server.services,
        owner_id=owner_id,
        run_id=accepted.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    thread_cancel = Mock(side_effect=AssertionError("thread-wide cancel must not run"))
    monkeypatch.setattr(server.app_runtime.agent_registry, "cancel_stream", thread_cancel)

    response = await client.post(
        f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=cancel_body(revision=2)
    )

    assert response.status_code == 201
    receipt = CancelRunReceipt.model_validate(response.json())
    assert receipt.status == "cancelRequested" and receipt.resultRunRevision == 3
    thread_cancel.assert_not_called()
    current = await client.get(f"{PREFIX}/runs/{accepted.runID}", headers=auth)
    assert current.status_code == 200
    assert current.json()["state"] == "queued"
    assert current.json()["dispatchPhase"] == "claimed"
    assert current.json()["cancelRequested"] is True
    task = server.services.ac_task_repo.get(str(accepted.taskID), owner_id)
    assert task is not None and task.active_run_id == str(accepted.runID)
    project = server.services.ac_project_repo.get(task.project_id, owner_id)
    assert project is not None and project.active_run_id == str(accepted.runID)


@pytest.mark.parametrize("interrupt_request", [False, True], ids=["response", "request-cancelled"])
async def test_durable_cancel_signals_exact_active_run_and_keeps_uncertain_lanes(
    env, monkeypatch, interrupt_request
):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    assert server.services is not None and server.app_runtime is not None
    services = server.services
    profile = get_capabilities(services)
    worker_profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(dispatch, "get_capabilities", lambda _: worker_profile)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        services,
        owner_id=owner_id,
        run_id=accepted.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    entered = asyncio.Event()
    durable_at_signal: list[bool] = []
    committed = threading.Event()
    release = threading.Event()
    if interrupt_request:
        original_cancel_run = agentcomputer.cancel_run

        def commit_then_wait(services, *, owner_id, run_id, command):
            receipt = original_cancel_run(
                services, owner_id=owner_id, run_id=run_id, command=command
            )
            committed.set()
            assert release.wait(timeout=5)
            return receipt

        monkeypatch.setattr(agentcomputer, "cancel_run", commit_then_wait)
    thread_cancel = Mock(side_effect=AssertionError("thread-wide cancel must not run"))
    monkeypatch.setattr(server.app_runtime.agent_registry, "cancel_stream", thread_cancel)

    async def hold_entry(invocation):
        result = await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)
        assert not result.replayed
        async with server.agentcomputer_active_runs.track(invocation):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with services.db.connect() as conn:
                    durable_at_signal.append(
                        conn.execute(
                            "SELECT COUNT(*) FROM ac_run_cancellations WHERE run_id=?",
                            (str(accepted.runID),),
                        ).fetchone()[0]
                        == 1
                    )
                raise

    handoff = asyncio.create_task(dispatch.dispatch_run(services, claim=claim, invoke=hold_entry))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        running = await client.get(f"{PREFIX}/runs/{accepted.runID}", headers=auth)
        assert running.status_code == 200
        assert running.json()["dispatchPhase"] == "engineAccepted"
        body = cancel_body(revision=running.json()["revision"])
        if interrupt_request:
            request = asyncio.create_task(
                client.post(f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=body)
            )
            assert await asyncio.to_thread(committed.wait, 5)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not handoff.done()
            release.set()
            stored = await client.get(f"{PREFIX}/commands/{body['commandID']}", headers=auth)
            assert stored.status_code == 200
            receipt = CancelRunReceipt.model_validate(stored.json())
        else:
            response = await client.post(
                f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=body
            )
            assert response.status_code == 201
            receipt = CancelRunReceipt.model_validate(response.json())
        assert receipt.status == "cancelRequested"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(handoff), timeout=5)
    finally:
        release.set()
        if not handoff.done():
            handoff.cancel()
            with suppress(asyncio.CancelledError):
                await handoff

    thread_cancel.assert_not_called()
    assert durable_at_signal == [True]
    current = await client.get(f"{PREFIX}/runs/{accepted.runID}", headers=auth)
    assert current.status_code == 200
    assert current.json()["state"] == "interrupted"
    assert current.json()["reconciliationRequired"] is True
    assert current.json()["cancelRequested"] is True
    task = services.ac_task_repo.get(str(accepted.taskID), owner_id)
    assert task is not None and task.active_run_id == str(accepted.runID)
    project = services.ac_project_repo.get(task.project_id, owner_id)
    assert project is not None and project.active_run_id == str(accepted.runID)


async def chunks(body: bytes):
    for offset in range(0, len(body), 1024):
        yield body[offset : offset + 1024]


async def test_cancel_body_limit_rejects_stream_before_domain(env, monkeypatch):
    client, server, auth = env
    operation = Mock(side_effect=AssertionError("oversized input reached cancellation domain"))
    monkeypatch.setattr(agentcomputer, "cancel_run", operation)
    response = await client.post(
        f"{PREFIX}/runs/{uuid4()}/cancel",
        headers={**auth, "content-type": "application/json", "content-length": "1"},
        content=chunks(b"x" * (MAX_COMMAND_BYTES + 1)),
    )
    assert response.status_code == 413
    assert response.json() == {"error": {"code": "payloadLimitExceeded", "retryable": False}}
    operation.assert_not_called()
    assert journal_counts(server) == (0, 0, 0)


async def test_corrupt_run_graph_fails_closed_without_new_cancel(env):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    accepted = await seed_run(client, server, auth, owner_id)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_turns SET snapshot_digest='sha256:private-digest-marker' WHERE turn_id=?",
            (str(accepted.turnID),),
        )
    before = journal_counts(server)
    response = await client.post(
        f"{PREFIX}/runs/{accepted.runID}/cancel", headers=auth, json=cancel_body()
    )
    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "storageUnavailable",
            "retryable": False,
            "reasonCode": "cancellationInvalid",
        }
    }
    assert "private-digest-marker" not in response.text
    assert journal_counts(server) == before


async def test_cancel_openapi_documents_typed_request_receipt_and_errors(tmp_path):
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"][f"{PREFIX}/runs/{{runID}}/cancel"]["post"]
        assert operation["summary"] and operation["tags"] == ["agentcomputer"]
        assert operation["security"] == [{"BearerAuth": []}]
        assert operation["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
            "/CancelRunCommand"
        )
        assert operation["responses"]["201"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/CancelRunReceipt")
        for status in ("401", "403", "404", "409", "413", "422", "503"):
            assert operation["responses"][status]["content"]["application/json"]["schema"][
                "$ref"
            ].endswith("/ProtocolErrorEnvelope")
        assert (await client.get("/api/docs")).status_code == 200
