"""Maintenance turns reject pauses and recover only after an explicit resume."""

from __future__ import annotations

import json

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import interactions, runtime, tasks
from octop.infra.projects.interaction_models import TaskControl
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "compaction-control.db")
    run_migrations(db)
    repos = RepoBundle.from_pool(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at) VALUES('task-one',1,'Compact history','task-request',"
            "'turn-one',100,100)"
        )
        repos.project_task_turn_repo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="turn-one",
                task_id="task-one",
                request_id="compact-request",
                prompt="/compact",
                reply="",
                status="queued",
                error_code="",
                activity="",
                configuration_json=json.dumps(
                    {"attachment_ids": [], "action_timeline_enabled": False}
                ),
                model_ref="provider/model",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    repos.secret_repo.get_or_create("jwt", lambda: b"compaction-control-test-secret")
    try:
        yield SharedServices(PathLayout(tmp_path), OctopConfig(), repos)
    finally:
        db.close()


def control(services, *, paused, request_id="control-request"):
    interactions.control(
        services,
        1,
        "task-one",
        TaskControl(turn_id="turn-one", request_id=request_id),
        paused=paused,
    )


@pytest.mark.parametrize("claimed", [False, True])
def test_compaction_pause_is_rejected_without_saving_an_intent(services, claimed):
    if claimed:
        assert services.project_task_turn_repo.claim("worker-one", runtime.now_ms())
    before = services.project_task_turn_repo.get("turn-one")
    with pytest.raises(OctopError) as rejected:
        control(services, paused=True)
    assert rejected.value.code == ErrorCode.TASK_CONTROL_INVALID
    assert services.project_task_turn_repo.get("turn-one") == before


@pytest.mark.parametrize(
    ("prompt", "attachment_ids"),
    [("/compact with instructions", []), ("/compact", ["attachment-one"]), ("/Compact", [])],
)
def test_ordinary_messages_keep_existing_pause_control(services, prompt, attachment_ids):
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET prompt=?,configuration_json=? WHERE turn_id='turn-one'",
            (prompt, json.dumps({"attachment_ids": attachment_ids})),
        )
    control(services, paused=True)
    turn = services.project_task_turn_repo.get("turn-one")
    assert turn.pause_requested is True
    assert len(json.loads(turn.requests_json)) == 1


async def test_stale_compaction_resumes_same_turn_without_a_native_checkpoint(
    services, monkeypatch
):
    original = services.project_task_turn_repo.claim("dead-worker", runtime.now_ms() - 100_000)
    assert original is not None and original.checkpoint_id == ""
    worker = runtime.ProjectTaskWorker(services)
    worker._recover()
    parked = services.project_task_turn_repo.get(original.turn_id)
    recovery = json.loads(parked.recovery_json)
    assert parked.is_waiting and parked.worker_id == "" and parked.wait_kind == "interrupted"
    assert recovery["mode"] == "prepare" and recovery["can_resume"] is True
    assert recovery["needs_reconciliation"] is False
    assert services.project_task_turn_repo.claim("not-authorized", runtime.now_ms()) is None
    assert services.project_task_repo.get("task-one", 1).active_turn_id == original.turn_id

    control(services, paused=False, request_id="resume-maintenance")
    resumed = services.project_task_turn_repo.claim("replacement-worker", runtime.now_ms())
    assert resumed is not None and resumed.turn_id == original.turn_id
    assert resumed.request_id == original.request_id and resumed.checkpoint_id == ""
    assert json.loads(resumed.recovery_json)["mode"] == "prepare"
    # A retransmitted resume is idempotent after the replacement owns the lease.
    control(services, paused=False, request_id="resume-maintenance")
    calls = []

    async def finish_compaction(shared, owner, turn, history):
        calls.append((owner, turn.turn_id, json.loads(turn.recovery_json)["mode"]))
        yield {"type": "token", "content": "Compaction completed after recovery."}

    monkeypatch.setattr(runtime, "run_turn", finish_compaction)
    worker.worker_id = resumed.worker_id
    await worker._execute(resumed)
    done = services.project_task_turn_repo.get(original.turn_id)
    assert done.status == "completed" and done.reply == "Compaction completed after recovery."
    assert calls == [(1, original.turn_id, "prepare")]
    assert len(services.project_task_turn_repo.list("task-one")) == 1
    assert services.project_task_repo.get("task-one", 1).active_turn_id is None


def test_ordinary_stale_turn_still_requires_a_native_checkpoint(services):
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET prompt='Continue work' WHERE turn_id='turn-one'"
        )
    assert services.project_task_turn_repo.claim("dead-worker", runtime.now_ms() - 100_000)
    runtime.ProjectTaskWorker(services)._recover()
    parked = services.project_task_turn_repo.get("turn-one")
    recovery = json.loads(parked.recovery_json)
    assert recovery["mode"] == "graph" and recovery["can_resume"] is False
    with pytest.raises(OctopError) as rejected:
        control(services, paused=False)
    assert rejected.value.code == ErrorCode.TASK_CONTROL_INVALID


def test_interrupted_compaction_can_still_be_cancelled(services):
    assert services.project_task_turn_repo.claim("dead-worker", runtime.now_ms() - 100_000)
    runtime.ProjectTaskWorker(services)._recover()
    result = tasks.cancel(services, 1, "task-one", "turn-one")
    assert result.active_turn_id is None
    assert result.turns[0].status == "cancelled"
    with pytest.raises(OctopError) as rejected:
        control(services, paused=False)
    assert rejected.value.code == ErrorCode.TASK_CONTROL_INVALID
