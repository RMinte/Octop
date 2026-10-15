"""A Task can own its mapping without creating a hidden project."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest

from octop.config import OctopConfig
from octop.infra.agentcomputer import standalone_tasks
from octop.infra.agentcomputer.commands import (
    CommandAdmissionError,
    get_command_receipt,
)
from octop.infra.agentcomputer.models import (
    NewMessageCommand,
    StandaloneTaskCreateCommand,
)
from octop.infra.agentcomputer.standalone_tasks import create_standalone_task
from octop.infra.agentcomputer.submission import submit_new_message
from octop.infra.agentcomputer.task_listing import (
    InvalidTaskListCursor,
    _encode_cursor,
    list_tasks,
)
from octop.infra.agentcomputer.tasks import get_task
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
    registry = AgentManager(repos=services.repos, paths=paths)
    registry._start_agent = AsyncMock(side_effect=AssertionError("model must not start"))
    registry._complete_create_bootstrap = AsyncMock(side_effect=AssertionError("no bootstrap"))
    registry._proactive_scheduler = Mock()
    yield services, registry
    pool.close()


def command(**changes):
    data = {
        "commandID": str(uuid4()),
        "idempotencyKey": str(uuid4()),
        "expectedRestoreEpoch": 1,
        "taskID": str(uuid4()),
        "title": "没有项目的任务",
    }
    data.update(changes)
    return StandaloneTaskCreateCommand.model_validate(data)


async def test_standalone_task_has_own_agent_thread_and_session(control):
    services, registry = control
    request = command()
    receipt = await create_standalone_task(services, registry, owner_id=1, command=request)
    assert receipt.projectID is None
    assert receipt.preparationStateAtAcceptance == "pending"
    assert not receipt.executionReady
    view = get_task(services, 1, request.taskID)
    assert view is not None
    assert view.projectID is None and view.preparationState == "ready"
    assert view.sessionID == receipt.sessionID and not view.executionReady
    with services.db.connect() as conn:
        task = services.ac_task_repo.get_in_transaction(conn, str(request.taskID), 1)
        assert task is not None and task.project_id is None
        assert task.standalone_workspace_id and task.octop_thread_id
        agent = services.agent_repo.get_in_transaction(conn, task.standalone_agent_id)
        assert agent is not None and agent.managed_task_id == str(request.taskID)
        assert agent.managed_project_id is None and agent.last_state == "stopped"
        assert conn.execute("SELECT COUNT(*) FROM ac_projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    page = list_tasks(services, 1, None, 10, None)
    assert page is not None and [item.task.taskID for item in page.items] == [request.taskID]
    assert page.items[0].latestRun is None
    assert list_tasks(services, 2, None, 10, None).items == []
    assert get_task(services, 2, request.taskID) is None
    assert get_command_receipt(services, 1, receipt.commandID) == receipt
    registry._start_agent.assert_not_called()
    registry._complete_create_bootstrap.assert_not_called()


async def test_standalone_task_replay_keeps_original_receipt_and_mapping(control):
    services, registry = control
    request = command()
    first = await create_standalone_task(services, registry, owner_id=1, command=request)
    original = services.ac_command_repo.get(str(first.commandID), 1).receipt_json
    retry = request.model_copy(update={"commandID": uuid4()})
    second = await create_standalone_task(services, registry, owner_id=1, command=retry)
    assert second == first.model_copy(update={"replayed": True})
    assert services.ac_command_repo.get(str(first.commandID), 1).receipt_json == original
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_tasks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 1
    with pytest.raises(CommandAdmissionError, match="idempotencyConflict"):
        await create_standalone_task(
            services,
            registry,
            owner_id=1,
            command=request.model_copy(update={"title": "different"}),
        )


async def test_replay_retries_failed_preparation_without_new_receipt(control, monkeypatch):
    services, registry = control
    request = command()
    original_claim = standalone_tasks._claim_task_root
    calls = 0

    def temporarily_unavailable(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary workspace failure")
        return original_claim(*args)

    monkeypatch.setattr(standalone_tasks, "_claim_task_root", temporarily_unavailable)
    first = await create_standalone_task(services, registry, owner_id=1, command=request)
    assert get_task(services, 1, request.taskID).preparationState == "failed"
    original_bytes = services.ac_command_repo.get(str(first.commandID), 1).receipt_json
    retry = request.model_copy(update={"commandID": uuid4()})
    replay = await create_standalone_task(services, registry, owner_id=1, command=retry)
    assert replay == first.model_copy(update={"replayed": True})
    assert get_task(services, 1, request.taskID).preparationState == "ready"
    assert services.ac_command_repo.get(str(first.commandID), 1).receipt_json == original_bytes
    assert calls == 2


async def test_projectless_task_pages_keep_owner_and_scope(control):
    services, registry = control
    first, second = command(), command()
    await create_standalone_task(services, registry, owner_id=1, command=first)
    await create_standalone_task(services, registry, owner_id=1, command=second)
    first_page = list_tasks(services, 1, None, 1, None)
    assert first_page is not None and first_page.nextCursor is not None
    second_page = list_tasks(services, 1, None, 1, first_page.nextCursor)
    assert second_page is not None and second_page.nextCursor is None
    assert {first_page.items[0].task.taskID, second_page.items[0].task.taskID} == {
        first.taskID,
        second.taskID,
    }
    from octop.infra.agentcomputer.identity import get_or_create_identity

    identity = get_or_create_identity(services.paths.agentcomputer_identity)
    foreign_scope = _encode_cursor(
        box_id=identity.boxID,
        owner_id=1,
        project_id=uuid4(),
        restore_epoch=identity.restoreEpoch,
        sort_at=0,
        task_id=first.taskID,
    )
    with pytest.raises(InvalidTaskListCursor):
        list_tasks(services, 1, None, 1, foreign_scope)
    with pytest.raises(InvalidTaskListCursor):
        list_tasks(services, 2, None, 1, first_page.nextCursor)


async def test_projectless_task_pages_order_by_activity_with_stable_ties(control):
    services, registry = control
    requests = [command(taskID=str(UUID(int=number))) for number in (1, 2, 3)]
    for request in requests:
        await create_standalone_task(services, registry, owner_id=1, command=request)
    with services.db.transaction() as conn:
        for request, created_at, updated_at in zip(
            requests, (1000, 2000, 3000), (6000, 5000, 6000), strict=True
        ):
            conn.execute(
                "UPDATE ac_tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
                (created_at, updated_at, str(request.taskID)),
            )

    first = list_tasks(services, 1, None, 1, None)
    assert first is not None and first.nextCursor is not None
    encoded = base64.urlsafe_b64decode(first.nextCursor + "=" * (-len(first.nextCursor) % 4))
    cursor_payload = json.loads(encoded)
    assert cursor_payload["v"] == 2 and cursor_payload["updatedAt"] == 6000
    assert "createdAt" not in cursor_payload
    second = list_tasks(services, 1, None, 1, first.nextCursor)
    assert second is not None and second.nextCursor is not None
    third = list_tasks(services, 1, None, 1, second.nextCursor)
    assert third is not None and third.nextCursor is None
    assert [page.items[0].task.taskID for page in (first, second, third)] == [
        UUID(int=3),
        UUID(int=1),
        UUID(int=2),
    ]
    assert [page.items[0].task.updatedAt for page in (first, second, third)] == [
        datetime.fromtimestamp(value / 1000, UTC) for value in (6000, 6000, 5000)
    ]
    assert [page.items[0].task.createdAt for page in (first, second, third)] == [
        datetime.fromtimestamp(value / 1000, UTC) for value in (3000, 1000, 2000)
    ]


async def test_standalone_task_does_not_accept_turn_execution(control):
    services, registry = control
    request = command()
    await create_standalone_task(services, registry, owner_id=1, command=request)
    message = NewMessageCommand(
        commandID=uuid4(),
        idempotencyKey=str(uuid4()),
        expectedRestoreEpoch=1,
        contextMode="newMessage",
        userMessageID=uuid4(),
        expectedProjectRevision=1,
        prompt="hello",
        permissionMode="askForApproval",
    )
    with pytest.raises(CommandAdmissionError, match="runtimeUnavailable"):
        submit_new_message(services, owner_id=1, task_id=request.taskID, command=message)
    with services.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ac_runs").fetchone()[0] == 0
