"""Recovery only tightens persisted dispatches; public queries stay read-only."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from octop.infra.agentcomputer import dispatch, recovery
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import admit_new_message
from octop.infra.agentcomputer.models import NewMessageCommand, RunView
from tests.support.agentcomputer import register_ready_task
from tests.support.app import octop_client
from tests.support.auth import auth_header, bootstrap_admin, create_user, resolve_user_id

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


async def persisted_dispatch(client, server, auth, owner, phase, monkeypatch):
    task = await register_ready_task(client, server, auth, owner)
    assert server.services is not None
    command = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt="Recovery metadata fixture; no model is invoked",
        permissionMode="askForApproval",
    )
    receipt = await asyncio.to_thread(
        admit_new_message, server.services, owner_id=owner, task_id=task.taskID, command=command
    )
    monkeypatch.setattr(dispatch, "get_capabilities", available_profile)
    monkeypatch.setattr(dispatch, "text_task_available", lambda _services, *, model_ref: True)
    claim = await asyncio.to_thread(
        dispatch.claim_run,
        server.services,
        owner_id=owner,
        run_id=receipt.runID,
        claimant_id=uuid4(),
        expected_revision=1,
    )
    if phase != "claimed":
        # Use the real durable marker; never SQL-fabricate a progressed Run.
        invocation = await asyncio.to_thread(dispatch._begin_dispatch, server.services, claim)
        if phase == "engineAccepted":
            await asyncio.to_thread(dispatch.accept_engine_entry, server.services, invocation)
    return task, receipt


@pytest.mark.parametrize("phase", ["claimed", "dispatching", "engineAccepted"])
async def test_recovery_projects_original_phase_lane_and_receipt_without_user_cancel(
    env, monkeypatch, phase
) -> None:
    client, server, auth = env
    owner = await resolve_user_id(client, auth, "admin")
    task, receipt = await persisted_dispatch(client, server, auth, owner, phase, monkeypatch)
    assert server.services is not None
    original = server.services.ac_command_repo.get(str(receipt.commandID), owner)
    assert original is not None
    before = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert before.status_code == 200 and before.json()["dispatchPhase"] == phase
    report = await asyncio.to_thread(recovery.recover_dispatches, server.services)
    assert report.scanned == report.tightened == 1 and report.unchanged == 0
    current = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    assert current.status_code == 200 and current.headers["cache-control"] == "no-store"
    view = RunView.model_validate(current.json())
    assert view.state == "interrupted" and view.reconciliationRequired
    assert view.dispatchPhase == phase and view.executionEpoch == before.json()["executionEpoch"]
    assert view.restoreEpoch == before.json()["restoreEpoch"]
    assert view.revision > before.json()["revision"]
    assert len(current.json()) == 13 and set(current.json()) == set(RunView.model_fields)
    assert view.cancelRequested is False and "historyTurnID" not in current.json()
    assert str(server.paths.root) not in current.text
    queried = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
    assert queried.status_code == 200 and queried.json() == receipt.model_dump(mode="json")
    stored = server.services.ac_command_repo.get(str(receipt.commandID), owner)
    assert stored is not None and stored.receipt_json == original.receipt_json
    with server.services.db.connect() as conn:
        assert conn.execute(
            "SELECT active_run_id FROM ac_tasks WHERE task_id=?", (str(task.taskID),)
        ).fetchone()[0] == str(receipt.runID)
        assert conn.execute(
            "SELECT active_run_id FROM ac_projects WHERE project_id=?", (str(task.projectID),)
        ).fetchone()[0] == str(receipt.runID)
        events = [
            tuple(row)
            for row in conn.execute(
                "SELECT event_type,payload_json FROM ac_events WHERE run_id=? ORDER BY sequence",
                (str(receipt.runID),),
            ).fetchall()
        ]
    assert [event[0] for event in events].count("run.interrupted") == 1
    assert not any(event[0] == "run.cancelRequested" for event in events)
    repeated = await asyncio.to_thread(recovery.recover_dispatches, server.services)
    assert repeated.tightened == 0
    assert (
        await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
    ).json() == current.json()
    with server.services.db.connect() as conn:
        assert [
            tuple(row)
            for row in conn.execute(
                "SELECT event_type,payload_json FROM ac_events WHERE run_id=? ORDER BY sequence",
                (str(receipt.runID),),
            ).fetchall()
        ] == events
    profile = (await client.get(f"{PREFIX}/capabilities", headers=auth)).json()
    assert profile["capabilityRevision"] == 15 and profile["supportedPermissionModes"] == []
    assert profile["capabilities"]["recovery"] == {
        "supported": False,
        "available": False,
        "reasonCode": "notImplemented",
    }
    for action in ("recover", "claim"):
        assert (
            await client.post(f"{PREFIX}/runs/{receipt.runID}/{action}", headers=auth, json={})
        ).status_code in (404, 405)
    assert (
        await client.post(f"{PREFIX}/runs/{receipt.runID}/cancel", headers=auth, json={})
    ).status_code == 422


async def test_recovered_run_preserves_owner_and_admin_isolation(env, monkeypatch) -> None:
    client, server, admin = env
    alice = await create_user(client, admin, username="recovery-alice")
    bob = await create_user(client, admin, username="recovery-bob")
    owner = await resolve_user_id(client, admin, "recovery-alice")
    _task, receipt = await persisted_dispatch(client, server, alice, owner, "claimed", monkeypatch)
    assert server.services is not None
    await asyncio.to_thread(recovery.recover_dispatches, server.services)
    for auth in (bob, admin):
        denied = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
        missing = await client.get(f"{PREFIX}/runs/{uuid4()}", headers=auth)
        assert denied.status_code == missing.status_code == 404
        assert (
            denied.json()
            == missing.json()
            == {"error": {"code": "resourceNotFound", "retryable": False}}
        )
    own = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=alice)
    assert own.status_code == 200 and own.json()["reconciliationRequired"] is True


async def test_embedded_restart_defers_tightening_until_internal_scan(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "control"
    async with octop_client(home) as (client, server):
        await bootstrap_admin(client, home)
        auth = await auth_header(client)
        owner = await resolve_user_id(client, auth, "admin")
        _task, receipt = await persisted_dispatch(
            client, server, auth, owner, "engineAccepted", monkeypatch
        )
        before = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
        assert before.status_code == 200
    async with octop_client(home) as (client, server):
        auth = await auth_header(client)
        retained = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
        assert retained.status_code == 200 and retained.json() == before.json()
        assert server.services is not None
        report = await asyncio.to_thread(recovery.recover_dispatches, server.services)
        assert report.tightened == 1
        tightened = await client.get(f"{PREFIX}/runs/{receipt.runID}", headers=auth)
        assert tightened.status_code == 200
        assert tightened.json()["state"] == "interrupted"
        assert tightened.json()["dispatchPhase"] == "engineAccepted"
        assert tightened.json()["reconciliationRequired"] is True
        original = await client.get(f"{PREFIX}/commands/{receipt.commandID}", headers=auth)
        assert original.status_code == 200 and original.json() == receipt.model_dump(mode="json")
