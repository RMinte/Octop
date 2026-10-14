"""Receipt queries preserve admission facts without enabling command writes."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from octop.api.routers import agentcomputer
from octop.infra.agentcomputer import approvals, dispatch, pauses, questions
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    admit_new_message,
    canonical_json,
    content_digest,
)
from octop.infra.agentcomputer.models import (
    ApprovalDecisionCommand,
    CommandReceipt,
    NewMessageCommand,
    QuestionAnswerCommand,
    RunPauseContent,
    RunPauseDecisionReceipt,
    RunPauseRecord,
)
from octop.infra.server import OctopServer
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import auth_header, bootstrap_admin, create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


async def seed_receipt(client, server: OctopServer, auth, owner_id: int) -> CommandReceipt:
    """Register complete isolated facts through the kernel, without engine dispatch."""
    assert server.services is not None
    task = await register_ready_task(client, server, auth, owner_id)
    command = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt="Isolated receipt fixture",
        permissionMode="askForApproval",
    )
    return await asyncio.to_thread(
        admit_new_message, server.services, owner_id=owner_id, task_id=task.taskID, command=command
    )


async def seed_pending_pause(
    client, server: OctopServer, auth, owner_id: int, monkeypatch, kind: str
) -> RunPauseRecord:
    """Exercise trusted internal pause facts in a temporary DB, with no model call."""
    assert server.services is not None
    accepted = await seed_receipt(client, server, auth, owner_id)
    services = server.services
    profile = get_capabilities(services)
    available = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(dispatch, "get_capabilities", lambda _: available)
    monkeypatch.setattr(dispatch, "text_task_available", lambda _services, *, model_ref: True)
    monkeypatch.setattr(pauses, "get_capabilities", lambda _: available)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        services,
        owner_id=owner_id,
        run_id=accepted.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    invocations = []

    async def accept(invocation):
        invocations.append(invocation)
        await asyncio.to_thread(dispatch.accept_engine_entry, services, invocation)

    await dispatch.dispatch_run(
        services, claim=claim, invoke=accept, active_runs=server.agentcomputer_active_runs
    )
    assert len(invocations) == 1
    invocation = invocations[0]
    with services.db.transaction() as conn:
        conn.execute("UPDATE ac_runs SET state='running' WHERE run_id=?", (str(accepted.runID),))
        run = services.ac_run_repo.get_in_transaction(conn, str(accepted.runID), owner_id)
        assert run is not None
        invocation = invocation.model_copy(update={"expectedRunRevision": run.revision})

    payload = {"fixture": "trusted approval" if kind == "approval" else "trusted question"}
    content = RunPauseContent(
        schemaVersion=1,
        payloadJSON=canonical_json(payload),
        payloadDigest=content_digest(payload),
        workspaceDigest=content_digest({"workspace": str(invocation.intent.snapshot.workspaceID)}),
        policyVersion="fixture-policy-v1",
        capabilityVersion="fixture-capability-v1",
    )
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    if kind == "approval":
        return await asyncio.to_thread(
            approvals.request_approval,
            services,
            invocation,
            action=content,
            expires_at=expires_at,
        )
    return await asyncio.to_thread(
        questions.ask_question,
        services,
        invocation,
        question=content,
        expires_at=expires_at,
    )


async def seed_pause_receipt(
    client, server: OctopServer, auth, owner_id: int, monkeypatch, kind: str
) -> RunPauseDecisionReceipt:
    record = await seed_pending_pause(client, server, auth, owner_id, monkeypatch, kind)
    assert server.services is not None
    services = server.services
    if kind == "approval":
        command = ApprovalDecisionCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=record.intent.restoreEpoch,
            approvalID=record.intent.pauseID,
            runID=record.intent.runID,
            expectedRevision=record.revision,
            decision="approveOnce",
        )
        return await asyncio.to_thread(
            approvals.decide_approval,
            services,
            owner_id=owner_id,
            command=command,
            current_action=record.intent.content,
            authorize=lambda _: True,
        )
    command = QuestionAnswerCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=record.intent.restoreEpoch,
        questionID=record.intent.pauseID,
        runID=record.intent.runID,
        expectedRevision=record.revision,
        answer="继续",
    )
    return await asyncio.to_thread(
        questions.answer_question,
        services,
        owner_id=owner_id,
        command=command,
        current_question=record.intent.content,
        authorize=lambda _: True,
    )


async def test_own_receipt_is_original_durable_result(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_receipt(client, server, auth, owner_id)
    assert server.services is not None
    original = server.services.ac_command_repo.get(str(receipt.commandID), owner_id)
    for _ in range(2):
        response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == receipt.model_dump(mode="json")
        assert response.json()["replayed"] is False
        assert response.json()["runState"] == "queued"
        assert str(server.paths.root) not in response.text
    assert server.services.ac_command_repo.get(str(receipt.commandID), owner_id) == original
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ac_turns").fetchone()[0] == 1


async def test_other_owner_and_admin_cannot_read_receipt_or_identity(env) -> None:
    client, server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="receipt-owner")
    other_auth = await create_user(client, admin_auth, username="receipt-other")
    owner_id = await resolve_user_id(client, admin_auth, "receipt-owner")
    receipt = await seed_receipt(client, server, owner_auth, owner_id)
    own = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=owner_auth)
    assert own.status_code == 200
    # Lookup must not read identity unless an owned row exists.
    server.paths.agentcomputer_identity.write_text('{"private-marker":', encoding="utf-8")
    for auth in (other_auth, admin_auth):
        missing = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
        denied = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert denied.headers["cache-control"] == "no-store"


async def test_invalid_identifier_has_safe_scoped_validation_error(env) -> None:
    client, _server, auth = env
    response = await client.get(f"{PREFIX}/commands/private-input-marker", headers=auth)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert "private-input-marker" not in response.text
    legacy = await client.post("/api/auth/login", json={"username": "admin"})
    assert legacy.status_code == 422
    assert "detail" in legacy.json()


async def test_receipt_query_requires_header_bearer(env) -> None:
    client, server, auth = env
    endpoint = f"{PREFIX}/commands/{uuid4()}"
    token = auth["Authorization"].split(" ", 1)[1]
    requests = (
        await client.get(endpoint),
        await client.get(endpoint, params={"access_token": token}),
        await client.get(endpoint, params={"access_token": token}, headers=auth),
    )
    for response in requests:
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    assert not server.paths.agentcomputer_identity.exists()


async def test_receipt_survives_restart_and_keeps_acceptance_epoch(tmp_path: Path) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        owner_id = await resolve_user_id(client, auth, "admin")
        receipt = await seed_receipt(client, server, auth, owner_id)
        first = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert first.status_code == 200
        # Simulate only persisted metadata, not a working restore implementation.
        path = server.paths.agentcomputer_identity
        identity = json.loads(path.read_text(encoding="utf-8"))
        identity["restoreEpoch"] = 2
        path.write_text(json.dumps(identity), encoding="utf-8")
    async with octop_client(home) as (client, _server):
        auth = await auth_header(client)
        second = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert second.status_code == 200
        assert second.json() == first.json() == receipt.model_dump(mode="json")
        capabilities = await client.get(f"{PREFIX}/capabilities", headers=auth)
        assert capabilities.json()["restoreEpoch"] == 2
        assert second.json()["restoreEpoch"] == 1


def pause_facts(server: OctopServer) -> dict[str, list[tuple[object, ...]]]:
    assert server.services is not None
    with server.services.db.connect() as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("ac_commands", "ac_run_pauses", "ac_events", "ac_runs")
        }


def expected_pause_status(record: RunPauseRecord) -> dict[str, object]:
    intent = record.intent.model_dump(mode="json")
    stored = record.model_dump(mode="json")
    return {
        "pauseID": intent["pauseID"],
        "kind": intent["kind"],
        "runID": intent["runID"],
        "turnID": intent["turnID"],
        "phase": stored["phase"],
        "revision": stored["revision"],
        "restoreEpoch": intent["restoreEpoch"],
        "executionEpoch": intent["executionEpoch"],
        "expiresAt": intent["expiresAt"],
        "createdAt": stored["createdAt"],
        "updatedAt": stored["updatedAt"],
        "decisionCommandID": (
            str(record.receipt.commandID) if record.receipt is not None else None
        ),
    }


def pause_id_path(kind: str, pause_id: object) -> str:
    collection = "approvals" if kind == "approval" else "questions"
    return f"{PREFIX}/{collection}/{pause_id}"


async def test_run_pause_query_returns_null_without_creating_facts(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_receipt(client, server, auth, owner_id)
    before = pause_facts(server)

    for _ in range(2):
        response = await client.get(f"{PREFIX}/runs/{receipt.runID}/pause", headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() is None
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("phase", ["pending", "decided", "expired"])
async def test_run_pause_query_returns_only_safe_status(
    env, monkeypatch, kind: str, phase: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    assert server.services is not None
    if phase == "pending":
        record = await seed_pending_pause(client, server, auth, owner_id, monkeypatch, kind)
    else:
        receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, kind)
        if phase == "expired":
            assert await asyncio.to_thread(
                pauses.expire_pause,
                server.services,
                owner_id=owner_id,
                pause_id=receipt.pauseID,
            )
        record = await asyncio.to_thread(
            pauses.get_pause, server.services, owner_id=owner_id, pause_id=receipt.pauseID
        )
        assert record is not None
    assert record.phase == phase
    before = pause_facts(server)

    for _ in range(2):
        response = await client.get(f"{PREFIX}/runs/{record.intent.runID}/pause", headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == expected_pause_status(record)
        assert "trusted approval" not in response.text
        assert "trusted question" not in response.text
        assert "fixture-policy-v1" not in response.text
        assert "fixture-capability-v1" not in response.text
        assert str(server.paths.root) not in response.text
    assert pause_facts(server) == before


async def test_run_pause_query_is_owner_scoped_even_with_invalid_identity(env, monkeypatch) -> None:
    client, server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username="run-pause-owner")
    other_auth = await create_user(client, admin_auth, username="run-pause-other")
    owner_id = await resolve_user_id(client, admin_auth, "run-pause-owner")
    record = await seed_pending_pause(client, server, owner_auth, owner_id, monkeypatch, "approval")
    endpoint = f"{PREFIX}/runs/{record.intent.runID}/pause"
    own = await client.get(endpoint, headers=owner_auth)
    assert own.status_code == 200
    assert own.json() == expected_pause_status(record)
    before = pause_facts(server)

    server.paths.agentcomputer_identity.write_text('{"private-marker":', encoding="utf-8")
    for auth in (other_auth, admin_auth):
        missing = await client.get(f"{PREFIX}/runs/{uuid4()}/pause", headers=auth)
        denied = await client.get(endpoint, headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert denied.headers["cache-control"] == "no-store"
        assert "private-marker" not in denied.text
    assert pause_facts(server) == before


async def test_run_pause_query_requires_header_auth_and_valid_run_uuid(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_receipt(client, server, auth, owner_id)
    endpoint = f"{PREFIX}/runs/{receipt.runID}/pause"
    token = auth["Authorization"].split(" ", 1)[1]
    before = pause_facts(server)

    for response in (
        await client.get(endpoint),
        await client.get(endpoint, params={"access_token": token}),
        await client.get(endpoint, params={"access_token": token}, headers=auth),
    ):
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    invalid = await client.get(f"{PREFIX}/runs/private-input-marker/pause", headers=auth)
    assert invalid.status_code == 422
    assert invalid.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert invalid.headers["cache-control"] == "no-store"
    assert "private-input-marker" not in invalid.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("damage", ["intent", "decisionReceipt", "decisionEvent"])
async def test_run_pause_query_returns_safe_503_for_damaged_graph_without_writes(
    env, monkeypatch, damage: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, "question")
    assert server.services is not None
    with server.services.db.transaction() as conn:
        if damage == "intent":
            conn.execute(
                "UPDATE ac_run_pauses SET intent_json=? WHERE pause_id=?",
                ('{"private-marker":', str(receipt.pauseID)),
            )
        elif damage == "decisionReceipt":
            conn.execute(
                "UPDATE ac_run_pauses SET decision_receipt_json=? WHERE pause_id=?",
                ('{"private-marker":', str(receipt.pauseID)),
            )
        else:
            conn.execute(
                "DELETE FROM ac_events WHERE run_id=? AND event_type='question.answered'",
                (str(receipt.runID),),
            )
    before = pause_facts(server)

    response = await client.get(f"{PREFIX}/runs/{receipt.runID}/pause", headers=auth)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["error"]["code"] == "storageUnavailable"
    assert response.json()["error"]["retryable"] is False
    assert "private-marker" not in response.text
    assert str(server.paths.root) not in response.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("phase", ["pending", "decided", "expired"])
async def test_pause_id_query_returns_only_owned_safe_status(
    env, monkeypatch, kind: str, phase: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    assert server.services is not None
    if phase == "pending":
        record = await seed_pending_pause(client, server, auth, owner_id, monkeypatch, kind)
    else:
        receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, kind)
        if phase == "expired":
            assert await asyncio.to_thread(
                pauses.expire_pause,
                server.services,
                owner_id=owner_id,
                pause_id=receipt.pauseID,
            )
        record = await asyncio.to_thread(
            pauses.get_pause, server.services, owner_id=owner_id, pause_id=receipt.pauseID
        )
        assert record is not None
    before = pause_facts(server)
    endpoint = pause_id_path(kind, record.intent.pauseID)
    for _ in range(2):
        response = await client.get(endpoint, headers=auth)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == expected_pause_status(record)
        assert "trusted approval" not in response.text
        assert "trusted question" not in response.text
        assert "fixture-policy-v1" not in response.text
        assert "fixture-capability-v1" not in response.text
        assert str(server.paths.root) not in response.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_pause_id_query_hides_unknown_wrong_kind_and_other_owner_even_with_invalid_identity(
    env, monkeypatch, kind: str
) -> None:
    client, server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username=f"{kind}-status-owner")
    other_auth = await create_user(client, admin_auth, username=f"{kind}-status-other")
    owner_id = await resolve_user_id(client, admin_auth, f"{kind}-status-owner")
    record = await seed_pending_pause(client, server, owner_auth, owner_id, monkeypatch, kind)
    endpoint = pause_id_path(kind, record.intent.pauseID)
    opposite = pause_id_path(
        "question" if kind == "approval" else "approval", record.intent.pauseID
    )
    own = await client.get(endpoint, headers=owner_auth)
    assert own.status_code == 200 and own.json() == expected_pause_status(record)
    before = pause_facts(server)

    server.paths.agentcomputer_identity.write_text('{"private-marker":', encoding="utf-8")
    missing = await client.get(pause_id_path(kind, uuid4()), headers=owner_auth)
    wrong_kind = await client.get(opposite, headers=owner_auth)
    foreign = await client.get(endpoint, headers=other_auth)
    admin = await client.get(endpoint, headers=admin_auth)
    for response in (missing, wrong_kind, foreign, admin):
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "resourceNotFound", "retryable": False}}
        assert response.headers["cache-control"] == "no-store"
        assert "private-marker" not in response.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_pause_id_query_requires_bearer_header_and_uuid(env, monkeypatch, kind: str) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    record = await seed_pending_pause(client, server, auth, owner_id, monkeypatch, kind)
    endpoint = pause_id_path(kind, record.intent.pauseID)
    token = auth["Authorization"].split(" ", 1)[1]
    before = pause_facts(server)
    for response in (
        await client.get(endpoint),
        await client.get(endpoint, params={"access_token": token}),
        await client.get(endpoint, params={"access_token": token}, headers=auth),
    ):
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "authenticationRequired", "retryable": False}}
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
    invalid = await client.get(pause_id_path(kind, "private-input-marker"), headers=auth)
    assert invalid.status_code == 422
    assert invalid.json() == {"error": {"code": "invalidRequest", "retryable": False}}
    assert invalid.headers["cache-control"] == "no-store"
    assert "private-input-marker" not in invalid.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("damage", ["intent", "decisionReceipt", "decisionEvent"])
async def test_pause_id_query_rejects_damaged_graph_without_writes(
    env, monkeypatch, kind: str, damage: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, kind)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        if damage == "intent":
            conn.execute(
                "UPDATE ac_run_pauses SET intent_json=? WHERE pause_id=?",
                ('{"private-marker":', str(receipt.pauseID)),
            )
        elif damage == "decisionReceipt":
            conn.execute(
                "UPDATE ac_run_pauses SET decision_receipt_json=? WHERE pause_id=?",
                ('{"private-marker":', str(receipt.pauseID)),
            )
        else:
            conn.execute(
                "DELETE FROM ac_events WHERE run_id=? AND event_type=?",
                (
                    str(receipt.runID),
                    "approval.decided" if kind == "approval" else "question.answered",
                ),
            )
    before = pause_facts(server)
    response = await client.get(pause_id_path(kind, receipt.pauseID), headers=auth)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "pauseInvalid"}
    }
    assert "private-marker" not in response.text
    assert str(server.paths.root) not in response.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_pause_id_query_survives_expiry_epoch_and_restart(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        owner_id = await resolve_user_id(client, auth, "admin")
        receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, kind)
        assert server.services is not None
        assert await asyncio.to_thread(
            pauses.expire_pause, server.services, owner_id=owner_id, pause_id=receipt.pauseID
        )
        identity_path = server.paths.agentcomputer_identity
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["restoreEpoch"] = 2
        identity_path.write_text(json.dumps(identity), encoding="utf-8")
        first = await client.get(pause_id_path(kind, receipt.pauseID), headers=auth)
        assert first.status_code == 200 and first.json()["phase"] == "expired"
        assert first.json()["restoreEpoch"] == 1
    async with octop_client(home) as (client, server):
        auth = await auth_header(client)
        before = pause_facts(server)
        second = await client.get(pause_id_path(kind, receipt.pauseID), headers=auth)
        assert second.status_code == 200 and second.json() == first.json()
        assert pause_facts(server) == before


@pytest.mark.parametrize("kind", ["approval", "question"])
async def test_pause_decision_receipt_http_is_original_and_owner_scoped(env, monkeypatch, kind):
    client, server, admin_auth = env
    owner_auth = await create_user(client, admin_auth, username=f"pause-{kind}-owner")
    other_auth = await create_user(client, admin_auth, username=f"pause-{kind}-other")
    owner_id = await resolve_user_id(client, admin_auth, f"pause-{kind}-owner")
    receipt = await seed_pause_receipt(client, server, owner_auth, owner_id, monkeypatch, kind)
    before = pause_facts(server)
    own = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=owner_auth)
    assert own.status_code == 200 and own.headers["cache-control"] == "no-store"
    assert own.json() == receipt.model_dump(mode="json")
    assert set(own.json()) == set(RunPauseDecisionReceipt.model_fields)
    assert str(server.paths.root) not in own.text
    assert pause_facts(server) == before

    # An unrelated caller must not be able to distinguish this UUID from a missing UUID,
    # even when the installation identity is broken.
    server.paths.agentcomputer_identity.write_text('{"private-marker":', encoding="utf-8")
    for auth in (other_auth, admin_auth):
        missing = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
        denied = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
        assert "private-marker" not in denied.text
    assert pause_facts(server) == before


async def test_pause_decision_receipt_http_survives_expiry_epoch_and_restart(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        owner_id = await resolve_user_id(client, auth, "admin")
        receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, "approval")
        assert server.services is not None
        await asyncio.to_thread(
            pauses.expire_pause, server.services, owner_id=owner_id, pause_id=receipt.pauseID
        )
        path = server.paths.agentcomputer_identity
        identity = json.loads(path.read_text(encoding="utf-8"))
        identity["restoreEpoch"] = 2
        path.write_text(json.dumps(identity), encoding="utf-8")
        first = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert first.status_code == 200 and first.json() == receipt.model_dump(mode="json")
        first_pause = await client.get(f"{PREFIX}/runs/{receipt.runID}/pause", headers=auth)
        assert first_pause.status_code == 200 and first_pause.json()["phase"] == "expired"
    async with octop_client(home) as (client, server):
        auth = await auth_header(client)
        before = pause_facts(server)
        second = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert second.status_code == 200 and second.json() == receipt.model_dump(mode="json")
        assert second.json()["restoreEpoch"] == 1
        second_pause = await client.get(f"{PREFIX}/runs/{receipt.runID}/pause", headers=auth)
        assert second_pause.status_code == 200 and second_pause.json() == first_pause.json()
        assert pause_facts(server) == before


@pytest.mark.parametrize("damage", ["receipt", "decisionEvent"])
async def test_corrupt_pause_decision_receipt_http_returns_safe_503_without_writes(
    env, monkeypatch, damage
):
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_pause_receipt(client, server, auth, owner_id, monkeypatch, "question")
    assert server.services is not None
    with server.services.db.transaction() as conn:
        if damage == "receipt":
            conn.execute(
                "UPDATE ac_run_pauses SET decision_receipt_json=? WHERE pause_id=?",
                ('{"private-marker":', str(receipt.pauseID)),
            )
        else:
            conn.execute(
                "DELETE FROM ac_events WHERE run_id=? AND event_type='question.answered'",
                (str(receipt.runID),),
            )
    before = pause_facts(server)
    response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert response.status_code == 503 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "receiptInvalid"}
    }
    assert "private-marker" not in response.text
    assert str(server.paths.root) not in response.text
    assert pause_facts(server) == before


@pytest.mark.parametrize("invalid", ['{"private-marker":', '{"accepted":true}'])
async def test_invalid_stored_receipt_returns_safe_503_without_replacement(
    env, invalid: str
) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_receipt(client, server, auth, owner_id)
    assert server.services is not None
    with server.services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_commands SET receipt_json = ? WHERE command_id = ?",
            (invalid, str(receipt.commandID)),
        )
    response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "receiptInvalid"}
    }
    assert "private-marker" not in response.text
    assert str(server.paths.root) not in response.text
    stored = server.services.ac_command_repo.get(str(receipt.commandID), owner_id)
    assert stored is not None
    assert stored.receipt_json == invalid


async def test_invalid_identity_returns_safe_503_for_owned_receipt(env) -> None:
    client, server, auth = env
    owner_id = await resolve_user_id(client, auth, "admin")
    receipt = await seed_receipt(client, server, auth, owner_id)
    invalid = b'{"private-marker":'
    path = server.paths.agentcomputer_identity
    path.write_bytes(invalid)
    response = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "storageUnavailable", "retryable": False, "reasonCode": "identityInvalid"}
    }
    assert path.read_bytes() == invalid
    assert "private-marker" not in response.text


async def test_storage_failure_returns_retryable_safe_503(env, monkeypatch) -> None:
    client, _server, auth = env

    def unavailable(*_args: object) -> None:
        raise OSError("private-storage-marker")

    monkeypatch.setattr(agentcomputer, "get_command_receipt", unavailable)
    response = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "storageUnavailable", "retryable": True}}
    assert response.headers["cache-control"] == "no-store"
    assert "private-storage-marker" not in response.text


async def test_database_failure_returns_retryable_safe_503(env, monkeypatch) -> None:
    client, server, auth = env
    assert server.services is not None

    def unavailable(*_args: object) -> None:
        raise sqlite3.OperationalError("private-sql-marker SELECT secret FROM ac_commands")

    monkeypatch.setattr(server.services.ac_command_repo, "get", unavailable)
    response = await client.get(f"{PREFIX}/commands/{uuid4()}", headers=auth)
    assert response.status_code == 503
    assert response.json() == {"error": {"code": "storageUnavailable", "retryable": True}}
    assert response.headers["cache-control"] == "no-store"
    assert "private-sql-marker" not in response.text
    assert "SELECT" not in response.text


async def test_command_collection_writes_remain_unavailable(env) -> None:
    client, server, auth = env
    for path in (f"{PREFIX}/commands",):
        response = await client.post(path, headers=auth, json={})
        # An unmatched POST can be rejected by the dashboard's static mount.
        assert response.status_code in (404, 405)
        expected_code = "resourceNotFound" if response.status_code == 404 else "invalidRequest"
        assert response.json()["error"]["code"] == expected_code
    response = await client.post(f"{PREFIX}/commands/{uuid4()}", headers=auth, json={})
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalidRequest"
    assert response.headers["allow"] == "GET"
    assert server.services is not None
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_commands").fetchone()[0] == 0


async def test_openapi_documents_receipt_as_immutable_acceptance(tmp_path: Path) -> None:
    home = tmp_path / "control"
    write_octop_config(home, enable_api_docs=True)
    async with octop_client(home) as (client, _server):
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"][f"{PREFIX}/commands/{{commandID}}"]["get"]
        assert operation["summary"]
        assert operation["tags"] == ["agentcomputer"]
        assert operation["security"] == [{"BearerAuth": []}]
        parameter = next(p for p in operation["parameters"] if p["in"] == "path")
        assert parameter["name"] == "commandID"
        assert parameter["schema"]["format"] == "uuid"
        assert "current Run state" in operation["description"]
        response = operation["responses"]["200"]["content"]["application/json"]
        assert {item["$ref"].rsplit("/", 1)[1] for item in response["schema"]["anyOf"]} == {
            "CommandReceipt",
            "ProjectCreateReceipt",
            "TaskCreateReceipt",
            "CancelRunReceipt",
            "RunPauseDecisionReceipt",
        }
        example = response["example"]
        assert CommandReceipt.model_validate(example).runState == "queued"
        assert example["accepted"] is True
        assert example["replayed"] is False
        for status in ("401", "403", "404", "422", "503"):
            error = operation["responses"][status]["content"]["application/json"]["schema"]
            assert error["$ref"].endswith("/ProtocolErrorEnvelope")
        assert "post" not in schema["paths"][f"{PREFIX}/commands/{{commandID}}"]
        pause = schema["paths"][f"{PREFIX}/runs/{{runID}}/pause"]["get"]
        assert pause["summary"] and pause["tags"] == ["agentcomputer"]
        assert pause["security"] == [{"BearerAuth": []}]
        assert pause["parameters"][0]["schema"]["format"] == "uuid"
        pause_response = pause["responses"]["200"]["content"]["application/json"]["schema"]
        assert any(
            item.get("$ref", "").endswith("/RunPauseStatus") for item in pause_response["anyOf"]
        )
        assert set(schema["components"]["schemas"]["RunPauseStatus"]["properties"]) == {
            "pauseID",
            "kind",
            "runID",
            "turnID",
            "phase",
            "revision",
            "restoreEpoch",
            "executionEpoch",
            "expiresAt",
            "createdAt",
            "updatedAt",
            "decisionCommandID",
        }
        for collection, parameter_name in (
            ("approvals", "approvalID"),
            ("questions", "questionID"),
        ):
            path = f"{PREFIX}/{collection}/{{{parameter_name}}}"
            operation = schema["paths"][path]["get"]
            assert operation["summary"] and operation["tags"] == ["agentcomputer"]
            assert operation["security"] == [{"BearerAuth": []}]
            parameter = next(item for item in operation["parameters"] if item["in"] == "path")
            assert parameter["name"] == parameter_name
            assert parameter["schema"]["format"] == "uuid"
            status_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
            assert status_schema["$ref"].endswith("/RunPauseStatus")
            for status in ("401", "403", "404", "422", "503"):
                error_schema = operation["responses"][status]["content"]["application/json"][
                    "schema"
                ]
                assert error_schema["$ref"].endswith("/ProtocolErrorEnvelope")
            assert "post" not in schema["paths"][path]
        assert (await client.get("/api/docs")).status_code == 200
