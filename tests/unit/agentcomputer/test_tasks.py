"""Atomic mapping fixtures; no production project readiness or model calls."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from octop.config import OctopConfig
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    StorageDataError,
    StorageUnavailableError,
    admit_new_message,
    get_command_receipt,
)
from octop.infra.agentcomputer.models import (
    NewMessageCommand,
    ProjectCreateCommand,
    TaskCreateCommand,
)
from octop.infra.agentcomputer.projects import create_project
from octop.infra.agentcomputer.tasks import get_task, register_task, task_command_digest
from octop.infra.agents.manager import AgentManager
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.services import build_shared_services
from octop.infra.gateway.threads import ThreadRegistry
from octop.infra.utils.paths import PathLayout


def request(project_id, **changes):
    data = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "expectedProjectRevision": 1,
        "projectID": str(project_id),
        "taskID": str(uuid4()),
        "title": " 中文  任务 " + "x" * 50,
    }
    data.update(changes)
    return TaskCreateCommand.model_validate(data)


async def ready_project(services, registry, owner_id=1):
    command = ProjectCreateCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        projectID=uuid4(),
        name="Mapping fixture " + str(uuid4()),
        configuration={"schemaVersion": 1, "modelRef": "local/qwen", "instructions": "fixture"},
    )
    await create_project(services, registry, owner_id=owner_id, command=command)
    # Only the temporary test database advances readiness. There is no
    # production API or configured Provider that performs this transition.
    with services.db.transaction() as conn:
        services.ac_project_repo.set_preparation(
            conn, str(command.projectID), owner_id, "ready", updated_at=1
        )
        services.ac_project_preparation_repo.clear_reason(
            conn, str(command.projectID), owner_id, updated_at=1
        )
    return command.projectID


@pytest.fixture
async def control(tmp_path):
    paths = PathLayout(tmp_path)
    pool = SqlitePool(paths.db)
    run_migrations(pool)
    services = build_shared_services(db=pool, paths=paths, config=OctopConfig())
    services.user_repo.create(username="one", password_hash="fixture", role="admin")
    services.user_repo.create(username="two", password_hash="fixture", role="user")
    registry = AgentManager(repos=services.repos, paths=paths)
    registry._start_agent = AsyncMock(side_effect=AssertionError("model must not start"))
    registry._complete_create_bootstrap = AsyncMock(side_effect=AssertionError("no bootstrap"))
    registry._proactive_scheduler = Mock()
    project_id = await ready_project(services, registry)
    yield services, registry, project_id
    pool.close()


def counts(services):
    with services.db.connect() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "ac_tasks",
                "threads",
                "sessions",
                "thread_history_projection",
                "ac_commands",
                "ac_turns",
                "ac_runs",
                "ac_events",
            )
        }


def test_golden_digest_keeps_identity_and_exact_title():
    command = request(
        "22222222-2222-4222-8222-222222222222",
        title=" 中文任务 ",
        taskID="33333333-3333-4333-8333-333333333333",
    )
    box_id = UUID("44444444-4444-4444-8444-444444444444")
    digest = task_command_digest(box_id, command)
    assert digest == "sha256:4d03dfc26604ff86e872ecc51a0dfc22141abefd1f697304fb09ca27ff7d35ae"
    clone = request(
        command.projectID,
        **{
            **command.model_dump(mode="json"),
            "commandID": str(uuid4()),
            "idempotencyKey": "other",
        },
    )
    assert task_command_digest(box_id, clone) == digest
    assert task_command_digest(box_id, command.model_copy(update={"title": "中文任务"})) != digest


@pytest.mark.parametrize(
    "change",
    [
        {"title": " "},
        {"title": "a\nb"},
        {"title": "x" * 257},
        {"expectedRestoreEpoch": True},
        {"expectedProjectRevision": "1"},
        {"ownerID": 1},
        {"octopThreadID": "thr_foreign"},
        {"clientSessionID": "local/file.jsonl"},
    ],
)
def test_strict_creation_input(change):
    with pytest.raises(ValidationError):
        request(uuid4(), **change)


def test_atomic_mapping_and_independent_dashboard_session(control):
    services, registry, project_id = control
    project = services.ac_project_repo.get(str(project_id), 1)
    legacy_key = ThreadRegistry.dashboard_key(agent_id=project.default_agent_id, user_id=1)
    services.thread_repo.insert(
        thread_id="thr_fixture_dashboard",
        agent_id=project.default_agent_id,
        user_id=1,
        channel_type="dashboard",
        session_key=legacy_key,
    )
    services.session_repo.upsert(
        session_key=legacy_key,
        agent_id=project.default_agent_id,
        user_id=1,
        channel_type="dashboard",
        chat_type="dm",
        thread_id="thr_fixture_dashboard",
    )
    command = request(project_id)
    receipt = register_task(services, owner_id=1, command=command)
    view = get_task(services, 1, receipt.taskID)
    assert view.title == command.title and view.sessionID == receipt.sessionID
    assert view.preparationState == receipt.preparationStateAtAcceptance == "ready"
    assert not receipt.executionReady and not view.executionReady
    assert services.session_repo.get(legacy_key).thread_id == "thr_fixture_dashboard"
    assert get_task(services, 2, receipt.taskID) is None
    assert get_command_receipt(services, 2, receipt.commandID) is None
    task = services.ac_task_repo.get(str(receipt.taskID), 1)
    thread = services.thread_repo.get(task.octop_thread_id)
    assert thread.session_key == ThreadRegistry.task_key(
        agent_id=project.default_agent_id, task_id=str(receipt.taskID)
    )
    assert task.next_sequence == task.revision == 1 and task.active_run_id is None
    assert services.ac_project_repo.get(str(project_id), 1).active_run_id is None
    assert counts(services)["ac_runs"] == counts(services)["ac_events"] == 0
    registry._start_agent.assert_not_called()
    registry._complete_create_bootstrap.assert_not_called()
    assert registry._harness_manager is None


def test_same_key_after_response_loss_uses_original_ids_without_alias(control):
    services, _, project_id = control
    command = request(project_id)
    first = register_task(services, owner_id=1, command=command)
    original = services.ac_command_repo.get(str(first.commandID), 1).receipt_json
    before = counts(services)
    retry = command.model_copy(update={"commandID": uuid4()})
    replay = register_task(services, owner_id=1, command=retry)
    assert replay == first.model_copy(update={"replayed": True})
    assert get_command_receipt(services, 1, first.commandID) == first
    assert get_command_receipt(services, 1, retry.commandID) is None
    assert services.ac_command_repo.get(str(first.commandID), 1).receipt_json == original
    assert counts(services) == before


@pytest.mark.parametrize("changed", ["title", "taskID", "expectedProjectRevision"])
def test_same_key_changed_request_conflicts(control, changed):
    services, _, project_id = control
    command = request(project_id)
    register_task(services, owner_id=1, command=command)
    value = {"title": "changed", "taskID": uuid4(), "expectedProjectRevision": 2}[changed]
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        register_task(services, owner_id=1, command=command.model_copy(update={changed: value}))
    assert counts(services) == before


def test_command_id_and_task_id_cannot_be_rebound(control):
    services, _, project_id = control
    command = request(project_id)
    first = register_task(services, owner_id=1, command=command)
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        register_task(
            services, owner_id=1, command=command.model_copy(update={"idempotencyKey": "new"})
        )
    with pytest.raises(CommandAdmissionError, match="resourceConflict"):
        register_task(services, owner_id=1, command=request(project_id, taskID=str(first.taskID)))
    assert counts(services) == before


@pytest.mark.parametrize("state", ["pending", "preparing", "failed"])
def test_unready_project_never_creates_threads(control, state):
    services, _, project_id = control
    with services.db.transaction() as conn:
        services.ac_project_repo.set_preparation(conn, str(project_id), 1, state, updated_at=1)
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
        register_task(services, owner_id=1, command=request(project_id))
    assert counts(services) == before


def test_wrong_owner_and_revision_do_not_change_mapping(control):
    services, _, project_id = control
    before = counts(services)
    with pytest.raises(CommandAdmissionError, match="resourceNotFound"):
        register_task(services, owner_id=2, command=request(project_id))
    with pytest.raises(CommandAdmissionError, match="revisionConflict"):
        register_task(services, owner_id=1, command=request(project_id, expectedProjectRevision=2))
    assert counts(services) == before


@pytest.mark.parametrize(
    "repo_name,method",
    [
        ("ac_task_repo", "insert"),
        ("thread_repo", "insert_in_transaction"),
        ("session_repo", "insert_in_transaction"),
        ("ac_task_repo", "bind_thread"),
        ("ac_command_repo", "insert"),
    ],
)
def test_each_write_failure_rolls_back_all_mapping_rows(control, monkeypatch, repo_name, method):
    services, _, project_id = control
    before = counts(services)
    repo = getattr(services, repo_name)
    original = getattr(repo, method)

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(repo, method, fail_after_write)
    with pytest.raises(RuntimeError, match="injected"):
        register_task(services, owner_id=1, command=request(project_id))
    assert counts(services) == before


def test_restart_and_two_connection_retry_reuse_one_mapping(control):
    services, _, project_id = control
    command = request(project_id)
    first = register_task(services, owner_id=1, command=command)
    pool = SqlitePool(services.paths.db)
    other = build_shared_services(db=pool, paths=services.paths, config=OctopConfig())
    try:
        assert get_command_receipt(other, 1, first.commandID) == first
        assert register_task(other, owner_id=1, command=command).replayed
        barrier = Barrier(2)
        second_command = request(project_id)

        def create(target):
            barrier.wait(timeout=10)
            return register_task(target, owner_id=1, command=second_command)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(create, (services, other)))
        assert sum(not item.replayed for item in results) == 1
        assert results[0].sessionID == results[1].sessionID
        assert (
            counts(services)["ac_tasks"]
            == counts(services)["threads"]
            == counts(services)["sessions"]
            == 2
        )
    finally:
        pool.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("sessionID", "55555555-5555-4555-8555-555555555555"),
        ("projectID", "66666666-6666-4666-8666-666666666666"),
        ("executionReady", 0),
        ("taskRevision", True),
    ],
)
def test_corrupt_receipt_rejected_by_lookup_and_replay(control, field, value):
    services, _, project_id = control
    command = request(project_id)
    receipt = register_task(services, owner_id=1, command=command)
    row = services.ac_command_repo.get(str(receipt.commandID), 1)
    body = json.loads(row.receipt_json)
    body[field] = value
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_commands SET receipt_json=? WHERE command_id=?",
            (json.dumps(body), str(receipt.commandID)),
        )
    with pytest.raises(StorageDataError):
        get_command_receipt(services, 1, receipt.commandID)
    with pytest.raises(StorageDataError):
        register_task(services, owner_id=1, command=command)


@pytest.mark.parametrize(
    "field", ["operation", "executionReady", "sessionID", "preparationStateAtAcceptance"]
)
def test_missing_original_receipt_fields_are_not_defaulted(control, field):
    services, _, project_id = control
    command = request(project_id)
    receipt = register_task(services, owner_id=1, command=command)
    raw = json.loads(services.ac_command_repo.get(str(receipt.commandID), 1).receipt_json)
    del raw[field]
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE ac_commands SET receipt_json=? WHERE command_id=?",
            (json.dumps(raw), str(receipt.commandID)),
        )
    with pytest.raises(StorageDataError):
        get_command_receipt(services, 1, receipt.commandID)


@pytest.mark.parametrize(
    "column,value",
    [
        ("channel_subject_id", None),
        ("channel_metadata", "{"),
        ("channel_metadata", None),
        ("agent_id", "foreign"),
    ],
)
def test_raw_session_corruption_is_not_repaired_on_read(control, column, value):
    services, _, project_id = control
    command = request(project_id)
    receipt = register_task(services, owner_id=1, command=command)
    if column == "agent_id":
        services.agent_repo.create(agent_id="foreign", user_id=2, name="foreign fixture")
    with services.db.transaction() as conn:
        conn.execute(f"UPDATE sessions SET {column}=? WHERE channel_type='agentcomputer'", (value,))
    with pytest.raises(StorageDataError):
        get_task(services, 1, receipt.taskID)
    with pytest.raises(StorageDataError):
        get_command_receipt(services, 1, receipt.commandID)


def test_old_epoch_mutation_fails_before_replay_but_original_facts_remain_readable(control):
    services, _, project_id = control
    command = request(project_id)
    receipt = register_task(services, owner_id=1, command=command)
    path = services.paths.agentcomputer_identity
    body = json.loads(path.read_text())
    body["restoreEpoch"] += 1
    path.write_text(json.dumps(body))
    with pytest.raises(CommandAdmissionError, match="restoreEpochConflict"):
        register_task(services, owner_id=1, command=command)
    assert get_command_receipt(services, 1, receipt.commandID) == receipt


def test_mapping_can_feed_internal_turn_fixture_without_dispatch(control):
    services, registry, project_id = control
    receipt = register_task(services, owner_id=1, command=request(project_id))
    turn = admit_new_message(
        services,
        owner_id=1,
        task_id=receipt.taskID,
        command=NewMessageCommand(
            commandID=uuid4(),
            idempotencyKey=str(uuid4()),
            expectedRestoreEpoch=1,
            expectedProjectRevision=1,
            contextMode="newMessage",
            userMessageID=uuid4(),
            prompt="fixture",
            permissionMode="askForApproval",
        ),
    )
    assert turn.runState == "queued" and turn.acceptedSequence == 1
    with services.db.connect() as conn:
        run = services.ac_run_repo.get_in_transaction(conn, str(turn.runID), 1)
    assert run.dispatch_phase == "notDispatched" and run.execution_epoch == 0
    assert get_task(services, 1, receipt.taskID).taskRevision == 2
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    registry._start_agent.assert_not_called()


def test_registration_does_not_borrow_another_connection_inside_owner_transaction(
    control, monkeypatch
):
    services, _, project_id = control
    for repo in (
        services.ac_project_repo,
        services.ac_project_preparation_repo,
        services.agent_repo,
        services.thread_repo,
        services.session_repo,
    ):
        monkeypatch.setattr(repo, "get", Mock(side_effect=AssertionError("nested connection")))
    receipt = register_task(services, owner_id=1, command=request(project_id))
    assert register_task(services, owner_id=1, command=request(project_id)).taskID != receipt.taskID


def test_storage_failure_is_explicit(control, monkeypatch):
    services, _, _ = control
    import sqlite3

    monkeypatch.setattr(
        services.ac_task_repo,
        "get_in_transaction",
        Mock(side_effect=sqlite3.OperationalError("private failure")),
    )
    with pytest.raises(StorageUnavailableError):
        get_task(services, 1, uuid4())
