"""Managed ingress and current Run reads without engine or model dispatch."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.agentcomputer import submission
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    get_command_receipt,
)
from octop.infra.agentcomputer.models import (
    NewMessageCommand,
    ProjectCreateCommand,
    TaskCreateCommand,
)
from octop.infra.agentcomputer.projects import create_project
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agentcomputer.submission import submit_new_message
from octop.infra.agentcomputer.tasks import register_task
from octop.infra.agents.manager import AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


def request(**changes):
    return NewMessageCommand.model_validate(
        {
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "contextMode": "newMessage",
            "userMessageID": str(uuid4()),
            "expectedProjectRevision": 1,
            "prompt": "  保存原始中文文本\n  ",
            "permissionMode": "askForApproval",
            **changes,
        }
    )


def counts(services):
    with services.db.connect() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ac_commands", "ac_turns", "ac_runs", "ac_events", "threads", "sessions")
        }


def allow_fixture(services, monkeypatch):
    """Only a trusted test getter substitutes availability; no transport knob."""
    profile = get_capabilities(services)
    profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={
                    "runs": CapabilityStatus(supported=True, available=True),
                }
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(submission, "get_capabilities", lambda _: profile)


@pytest.fixture
async def control(tmp_path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
    services.user_repo.create(username="owner", password_hash="fixture", role="admin")
    services.user_repo.create(username="other", password_hash="fixture", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    manager._start_agent = AsyncMock(side_effect=AssertionError("no model start"))
    manager._complete_create_bootstrap = AsyncMock(side_effect=AssertionError("no bootstrap"))
    manager._proactive_scheduler = Mock()
    project_id = uuid4()
    await create_project(
        services,
        manager,
        owner_id=1,
        command=ProjectCreateCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            projectID=project_id,
            name="Run fixture",
            configuration={
                "schemaVersion": 1,
                "modelRef": "fixture/no-model",
                "instructions": "fixture",
            },
        ),
    )
    with services.db.transaction() as conn:
        services.ac_project_repo.set_preparation(conn, str(project_id), 1, "ready", updated_at=1)
        services.ac_project_preparation_repo.clear_reason(conn, str(project_id), 1, updated_at=1)
    mapping = register_task(
        services,
        owner_id=1,
        command=TaskCreateCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            expectedProjectRevision=1,
            projectID=project_id,
            taskID=uuid4(),
            title="Run fixture",
        ),
    )
    yield services, manager, mapping.taskID
    manager._start_agent.assert_not_called()
    manager._complete_create_bootstrap.assert_not_called()
    assert manager._harness_manager is None
    pool.close()


def test_production_closed_gate_preserves_all_counts_and_lanes(control):
    services, _, task_id = control
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable") as error:
        submit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert error.value.reason == "executionUnavailable"
    assert counts(services) == before
    task = services.ac_task_repo.get(str(task_id), 1)
    assert task.revision == task.next_sequence == 1 and task.active_run_id is None
    assert services.ac_project_repo.get(task.project_id, 1).active_run_id is None


def test_unknown_and_cross_owner_are_indistinguishable_before_gate(control):
    services, _, task_id = control
    before = counts(services)
    for owner, target in ((1, uuid4()), (2, task_id)):
        with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
            submit_new_message(services, owner_id=owner, task_id=target, command=request())
    assert counts(services) == before


def test_fixture_accepts_once_and_replays_after_gate_closes(control, monkeypatch):
    services, _, task_id = control
    command = request()
    with monkeypatch.context() as enabled:
        allow_fixture(services, enabled)
        receipt = submit_new_message(services, owner_id=1, task_id=task_id, command=command)
    before = counts(services)
    raw = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    replay = submit_new_message(
        services,
        owner_id=1,
        task_id=task_id,
        command=command.model_copy(update={"commandID": uuid4()}),
    )
    assert replay == receipt.model_copy(update={"replayed": True})
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == raw
    assert counts(services) == before
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "queued" and view.dispatchPhase == "notDispatched"
    assert view.executionEpoch == 0 and view.revision == 1
    assert view.createdAt == view.updatedAt
    assert get_run(services, owner_id=2, run_id=receipt.runID) is None
    assert get_run(services, owner_id=1, run_id=uuid4()) is None


@pytest.mark.parametrize(
    "change",
    [
        {"permissionMode": "fullAccess"},
        {"permissionMode": "approveForMe"},
        {"selectedSkillIDs": ["unresolved-skill"]},
        {"inputArtifactIDs": ["55555555-5555-4555-8555-555555555555"]},
    ],
)
def test_unsupported_permission_and_resources_are_never_registered(control, monkeypatch, change):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="capabilityUnsupported"):
        submit_new_message(services, owner_id=1, task_id=task_id, command=request(**change))
    assert counts(services) == before


def test_damaged_raw_session_is_not_repaired_or_accepted(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    with services.db.transaction() as conn:
        conn.execute("UPDATE sessions SET channel_metadata=NULL WHERE channel_type='agentcomputer'")
    before = counts(services)
    with pytest.raises(StorageDataError):
        submit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert counts(services) == before
    with services.db.connect() as conn:
        assert conn.execute("SELECT channel_metadata FROM sessions").fetchone()[0] is None


def test_invalid_persisted_task_numeric_value_is_a_storage_error(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    with services.db.transaction() as conn:
        # SQLite's INTEGER affinity and CHECK(revision > 0) still accept text.
        conn.execute("UPDATE ac_tasks SET revision='invalid' WHERE task_id=?", (str(task_id),))
    before = counts(services)
    with pytest.raises(StorageDataError):
        submit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert counts(services) == before


def test_current_terminal_run_does_not_rewrite_queued_receipt(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    command = request()
    receipt = submit_new_message(services, owner_id=1, task_id=task_id, command=command)
    raw = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_runs SET state='succeeded',dispatch_phase='engineAccepted',execution_epoch=1,"
            "revision=4,updated_at=updated_at+1000 WHERE run_id=?",
            (str(receipt.runID),),
        )
        conn.execute("UPDATE ac_tasks SET active_run_id=NULL WHERE task_id=?", (str(task_id),))
        conn.execute(
            "UPDATE ac_projects SET active_run_id=NULL WHERE active_run_id=?", (str(receipt.runID),)
        )
    view = get_run(services, owner_id=1, run_id=receipt.runID)
    assert view.state == "succeeded" and view.dispatchPhase == "engineAccepted"
    assert view.revision == 4 and view.executionEpoch == 1
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert submit_new_message(services, owner_id=1, task_id=task_id, command=command).replayed
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == raw


def test_run_query_rejects_missing_original_command_without_repair(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    receipt = submit_new_message(services, owner_id=1, task_id=task_id, command=request())
    with services.db.transaction() as conn:
        conn.execute("DELETE FROM ac_commands WHERE command_id=?", (str(receipt.commandID),))
    before = counts(services)
    with pytest.raises(StorageDataError):
        get_run(services, owner_id=1, run_id=receipt.runID)
    assert counts(services) == before


def test_old_epoch_cannot_replay_but_historical_run_and_receipt_are_readable(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    command = request()
    receipt = submit_new_message(services, owner_id=1, task_id=task_id, command=command)
    path = services.paths.agentcomputer_identity
    content = json.loads(path.read_text())
    content["restoreEpoch"] = 2
    path.write_text(json.dumps(content))
    # Use production getter again so no test profile is stale.
    monkeypatch.setattr(submission, "get_capabilities", get_capabilities)
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        submit_new_message(services, owner_id=1, task_id=task_id, command=command)
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert get_run(services, owner_id=1, run_id=receipt.runID).restoreEpoch == 1
    assert counts(services) == before


def test_submission_and_run_query_do_not_borrow_nested_connections(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    for repo in (
        services.ac_command_repo,
        services.ac_project_repo,
        services.ac_project_preparation_repo,
        services.ac_task_repo,
        services.agent_repo,
        services.thread_repo,
        services.session_repo,
    ):
        if hasattr(repo, "get"):
            monkeypatch.setattr(repo, "get", Mock(side_effect=AssertionError("nested connection")))
    receipt = submit_new_message(services, owner_id=1, task_id=task_id, command=request())
    assert get_run(services, owner_id=1, run_id=receipt.runID).runID == receipt.runID


def test_concurrent_trusted_ingress_reuses_one_atomic_registration(control, monkeypatch):
    services, _, task_id = control
    allow_fixture(services, monkeypatch)
    command = request()
    pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
    barrier = Barrier(2)
    try:

        def submit(target):
            barrier.wait(timeout=10)
            return submit_new_message(target, owner_id=1, task_id=task_id, command=command)

        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(submit, (services, other)))
        assert sum(not item.replayed for item in receipts) == 1
        assert receipts[0].runID == receipts[1].runID
        assert counts(services)["ac_runs"] == counts(services)["ac_events"] == 1
    finally:
        pool.close()
