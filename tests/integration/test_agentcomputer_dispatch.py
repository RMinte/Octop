"""Public projections of internal dispatch fixtures never start a real model."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from octop.infra.agentcomputer import dispatch
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import CommandAdmissionError, admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand, RunView
from tests.support.agentcomputer import register_ready_task
from tests.support.auth import create_user, resolve_user_id

PREFIX = "/api/agentcomputer/v1"


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


async def registered_run(client, server, auth, owner_id):
    task = await register_ready_task(client, server, auth, owner_id)
    assert server.services is not None
    command = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt="Only application intake is simulated",
        permissionMode="askForApproval",
    )
    receipt = await asyncio.to_thread(
        admit_new_message, server.services, owner_id=owner_id, task_id=task.taskID, command=command
    )
    return task, receipt


async def test_run_get_projects_claim_dispatch_and_app_intake_without_started_claim(
    env, monkeypatch
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task, original = await registered_run(client, server, auth, owner)
    assert server.services is not None
    before = server.services.ac_command_repo.get(str(original.commandID), owner)
    assert before is not None
    immutable_json = before.receipt_json
    monkeypatch.setattr(dispatch, "get_capabilities", available_profile)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        server.services,
        owner_id=owner,
        run_id=original.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    claimed = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
    assert claimed.status_code == 200
    view = RunView.model_validate(claimed.json())
    assert view.state == "queued" and view.dispatchPhase == "claimed"
    assert view.executionEpoch == 1 and view.revision == claim.runRevision
    assert view.taskID == task.taskID and view.projectID == task.projectID
    entries = []

    async def invoke(invocation):
        # The handoff marker must be committed before the internal callback.
        handoff = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
        assert handoff.status_code == 200
        current = RunView.model_validate(handoff.json())
        assert current.state == "queued" and current.dispatchPhase == "dispatching"
        assert current.revision == invocation.expectedRunRevision
        result = await asyncio.to_thread(dispatch.accept_engine_entry, server.services, invocation)
        assert not result.replayed
        entries.append(result.receipt)

    entry = await dispatch.dispatch_run(
        server.services, claim=claim, invoke=invoke, active_runs=server.agentcomputer_active_runs
    )
    assert entries == [entry]
    assert entry.runID == original.runID and entry.turnID == original.turnID
    accepted = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
    assert accepted.status_code == 200
    current = RunView.model_validate(accepted.json())
    assert current.state == "queued" and current.dispatchPhase == "engineAccepted"
    assert current.executionEpoch == 1 and not current.reconciliationRequired
    assert set(accepted.json()) == set(RunView.model_fields)
    assert all(
        key not in accepted.json()
        for key in ("engineReceipt", "claimantID", "historyTurnID", "intent")
    )
    assert accepted.headers["cache-control"] == "no-store"

    async def never_reinvoke(_invocation):
        raise AssertionError("confirmed intake replay must not invoke another callback")

    replay = await dispatch.dispatch_run(
        server.services,
        claim=claim,
        invoke=never_reinvoke,
        active_runs=server.agentcomputer_active_runs,
    )
    assert replay == entry
    repeated = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
    assert repeated.json() == accepted.json()
    receipt = await client.get(f"{PREFIX}/commands/{original.commandID}", headers=auth)
    assert receipt.status_code == 200 and receipt.json() == original.model_dump(mode="json")
    stored = server.services.ac_command_repo.get(str(original.commandID), owner)
    assert stored is not None and stored.receipt_json == immutable_json

    latest = await asyncio.to_thread(
        dispatch.get_claim, server.services, owner_id=owner, run_id=original.runID
    )
    assert latest is not None and latest.engineReceipt == entry
    await asyncio.to_thread(dispatch.interrupt_uncertain_run, server.services, claim=latest)
    interrupted = await client.get(f"{PREFIX}/runs/{original.runID}", headers=auth)
    assert interrupted.status_code == 200
    blocked = RunView.model_validate(interrupted.json())
    assert blocked.state == "interrupted" and blocked.dispatchPhase == "engineAccepted"
    assert blocked.reconciliationRequired and blocked.revision > current.revision
    with server.services.db.connect() as conn:
        assert conn.execute(
            "SELECT active_run_id FROM ac_tasks WHERE task_id=?", (str(task.taskID),)
        ).fetchone()[0] == str(original.runID)
        assert conn.execute(
            "SELECT active_run_id FROM ac_projects WHERE project_id=?", (str(task.projectID),)
        ).fetchone()[0] == str(original.runID)


async def test_dispatch_projection_keeps_owner_isolation_including_admin(env, monkeypatch) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="dispatch-alice")
    bob = await create_user(client, admin, username="dispatch-bob")
    owner = await resolve_user_id(client, admin, "dispatch-alice")
    _task, receipt = await registered_run(client, server, alice, owner)
    assert server.services is not None
    monkeypatch.setattr(dispatch, "get_capabilities", available_profile)
    await asyncio.to_thread(
        dispatch.claim_run,
        server.services,
        owner_id=owner,
        run_id=receipt.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    for auth in (bob, admin):
        missing = await client.get(f"{PREFIX}/runs/{uuid4()}", headers=auth)
        denied = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
        assert missing.status_code == denied.status_code == 404
        assert (
            missing.json()
            == denied.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
    own = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=alice)
    assert own.status_code == 200 and own.json()["dispatchPhase"] == "claimed"


async def test_production_claim_gate_prevents_journal_state_and_event_writes(env) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    _task, receipt = await registered_run(client, server, auth, owner)
    assert server.services is not None
    before = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    with server.services.db.connect() as conn:
        events_before = conn.execute("SELECT COUNT(*) FROM ac_events").fetchone()[0]
    with pytest.raises(CommandAdmissionError) as failure:
        await asyncio.to_thread(
            dispatch.claim_run,
            server.services,
            owner_id=owner,
            run_id=receipt.runID,
            claimant_id=uuid4(),
            expected_revision=1,
        )
    assert (
        failure.value.code == "runtimeUnavailable"
        and failure.value.reason == "executionUnavailable"
    )
    after = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert after.status_code == 200 and after.json() == before.json()
    assert (
        await asyncio.to_thread(
            dispatch.get_claim, server.services, owner_id=owner, run_id=receipt.runID
        )
        is None
    )
    with server.services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_events").fetchone()[0] == events_before
    for action in ("claim", "dispatch", "recover"):
        response = await client.post(
            f"{PREFIX}/runs/{receipt.runID}/{action}", headers=auth, json={}
        )
        assert response.status_code in (404, 405)
