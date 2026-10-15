"""A projectless Task records its own immutable, queued conversation facts."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from tests.unit.agentcomputer.test_dispatch import allow

from octop.config import OctopConfig
from octop.infra.agentcomputer import dispatch, recovery, submission
from octop.infra.agentcomputer.capabilities import CapabilityStatus, get_capabilities
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    get_command_receipt,
)
from octop.infra.agentcomputer.event_listing import list_task_events
from octop.infra.agentcomputer.models import (
    StandaloneNewMessageCommand,
    StandaloneTaskCreateCommand,
)
from octop.infra.agentcomputer.runs import get_run
from octop.infra.agentcomputer.standalone_tasks import create_standalone_task
from octop.infra.agentcomputer.submission import submit_new_message
from octop.infra.agentcomputer.task_listing import list_tasks
from octop.infra.agentcomputer.task_snapshot import get_task_control_snapshot
from octop.infra.agentcomputer.tasks import get_task
from octop.infra.agentcomputer.turn_listing import list_turns
from octop.infra.agents.manager import AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def control(tmp_path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(
        db=pool,
        paths=paths,
        config=OctopConfig(agentcomputer_text_model_ref="local/qwen"),
    )
    services.user_repo.create(username="owner", password_hash="fixture", role="user")
    services.user_repo.create(username="other", password_hash="fixture", role="user")
    manager = AgentManager(repos=services.repos, paths=paths)
    manager._start_agent = AsyncMock(side_effect=AssertionError("no model start"))
    manager._complete_create_bootstrap = AsyncMock(side_effect=AssertionError("no bootstrap"))
    manager._proactive_scheduler = Mock()
    yield services, manager
    pool.close()


def _available(services, monkeypatch):
    profile = get_capabilities(services)
    profile = profile.model_copy(
        update={
            "capabilities": profile.capabilities.model_copy(
                update={"runs": CapabilityStatus(supported=True, available=True)}
            ),
            "supportedPermissionModes": ["askForApproval"],
        }
    )
    monkeypatch.setattr(submission, "get_capabilities", lambda _: profile)
    monkeypatch.setattr(submission, "text_task_available", lambda _services, *, model_ref: True)


async def _task(services, manager):
    task_id = uuid4()
    await create_standalone_task(
        services,
        manager,
        owner_id=1,
        command=StandaloneTaskCreateCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            taskID=task_id,
            title="独立会话",
        ),
    )
    task = get_task(services, 1, task_id)
    assert task is not None and task.preparationState == "ready"
    return task


def _message(task, **changes):
    return StandaloneNewMessageCommand.model_validate(
        {
            "commandID": str(uuid4()),
            "idempotencyKey": str(uuid4()),
            "expectedRestoreEpoch": 1,
            "contextMode": "newMessage",
            "userMessageID": str(uuid4()),
            "expectedTaskRevision": task.taskRevision,
            "prompt": "你好，记住这是同一个 Task。",
            "permissionMode": "askForApproval",
            **changes,
        }
    )


async def test_standalone_turn_is_atomic_and_readable_without_project(control, monkeypatch):
    services, manager = control
    task = await _task(services, manager)
    command = _message(task)
    with monkeypatch.context() as available:
        _available(services, available)
        receipt = submit_new_message(services, owner_id=1, task_id=task.taskID, command=command)
    with services.db.connect() as conn:
        turn = conn.execute(
            "SELECT * FROM ac_turns WHERE turn_id=?", (str(receipt.turnID),)
        ).fetchone()
        run = conn.execute("SELECT * FROM ac_runs WHERE run_id=?", (str(receipt.runID),)).fetchone()
        task_row = conn.execute(
            "SELECT * FROM ac_tasks WHERE task_id=?", (str(task.taskID),)
        ).fetchone()
        assert turn["project_id"] is None and turn["schema_version"] == 2
        assert run["project_id"] is None and run["schema_version"] == 2
        assert task_row["active_run_id"] == str(receipt.runID)
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    assert get_run(services, owner_id=1, run_id=receipt.runID).projectID is None
    assert get_run(services, owner_id=2, run_id=receipt.runID) is None
    page = list_tasks(services, 1, None, 10, None)
    assert page is not None and page.items[0].latestRun.runID == receipt.runID
    turns = list_turns(services, 1, task.taskID, 10, None)
    assert turns is not None and turns.items[0].prompt == command.prompt
    snapshot = get_task_control_snapshot(services, 1, task.taskID)
    assert snapshot is not None and snapshot.latestRun.runID == receipt.runID
    events = list_task_events(services, 1, task.taskID, 10, None)
    assert events is not None and [item.type for item in events.items] == ["run.accepted"]
    original = services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json
    replay = submit_new_message(
        services,
        owner_id=1,
        task_id=task.taskID,
        command=command.model_copy(update={"commandID": uuid4()}),
    )
    assert replay == receipt.model_copy(update={"replayed": True})
    assert services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json == original
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        submit_new_message(
            services,
            owner_id=1,
            task_id=task.taskID,
            command=command.model_copy(update={"prompt": "不同的消息"}),
        )
    manager._start_agent.assert_not_called()


async def test_standalone_turn_rejects_cross_owner_revision_and_active_run(control, monkeypatch):
    services, manager = control
    task = await _task(services, manager)
    command = _message(task)
    with monkeypatch.context() as available:
        _available(services, available)
        with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
            submit_new_message(services, owner_id=2, task_id=task.taskID, command=command)
        with pytest.raises(CommandAdmissionError, match="revisionConflict"):
            submit_new_message(
                services,
                owner_id=1,
                task_id=task.taskID,
                command=_message(task, expectedTaskRevision=task.taskRevision + 1),
            )
        submit_new_message(services, owner_id=1, task_id=task.taskID, command=command)
        current = get_task(services, 1, task.taskID)
        with pytest.raises(CommandAdmissionError, match="activeRunConflict"):
            submit_new_message(services, owner_id=1, task_id=task.taskID, command=_message(current))
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 1


async def test_standalone_turn_receipt_rejects_snapshot_tampering(control, monkeypatch):
    services, manager = control
    task = await _task(services, manager)
    with monkeypatch.context() as available:
        _available(services, available)
        receipt = submit_new_message(
            services, owner_id=1, task_id=task.taskID, command=_message(task)
        )
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_turns SET snapshot_digest=? WHERE turn_id=?",
            ("sha256:" + "0" * 64, str(receipt.turnID)),
        )
    with pytest.raises(StorageDataError, match="standalone Turn acceptance binding mismatch"):
        get_command_receipt(services, 1, receipt.commandID)
    with pytest.raises(StorageDataError):
        get_run(services, owner_id=1, run_id=receipt.runID)


@pytest.mark.parametrize("phase", ["claimed", "dispatching", "engineAccepted"])
async def test_standalone_claimed_run_is_fenced_on_restart_without_replay(
    control, monkeypatch, phase
):
    services, manager = control
    task = await _task(services, manager)
    with monkeypatch.context() as available:
        _available(services, available)
        receipt = submit_new_message(
            services, owner_id=1, task_id=task.taskID, command=_message(task)
        )
    allow(monkeypatch)
    claim = dispatch.claim_run(
        services, owner_id=1, run_id=receipt.runID, claimant_id=uuid4(), expected_revision=1
    )
    assert claim.intent.schemaVersion == 2 and claim.intent.projectID is None
    if phase != "claimed":
        invocation = dispatch._begin_dispatch(services, claim)
        if phase == "engineAccepted":
            dispatch.accept_engine_entry(services, invocation)
    report = recovery.recover_dispatches(services)
    assert report.scanned == 1 and report.tightened == 1
    current = get_run(services, owner_id=1, run_id=receipt.runID)
    assert current is not None and current.state == "interrupted"
    assert current.reconciliationRequired and current.dispatchPhase == phase
    persisted_task = services.ac_task_repo.get(str(task.taskID), 1)
    assert persisted_task is not None and persisted_task.active_run_id == str(receipt.runID)
    assert recovery.recover_dispatches(services).scanned == 0
    with services.db.connect() as conn:
        assert (
            services.ac_run_repo.list_queued_candidates(
                conn, str(claim.intent.boxID), after_pk=0, through_pk=10_000
            )
            == []
        )
    with pytest.raises(CommandAdmissionError):
        dispatch.claim_run(
            services,
            owner_id=1,
            run_id=receipt.runID,
            claimant_id=uuid4(),
            expected_revision=current.revision,
        )
