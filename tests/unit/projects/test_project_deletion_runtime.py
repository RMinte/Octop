"""Project deletion drains exact executions without hiding unresolved dispatches."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from threading import Event as ThreadEvent

import pytest

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_tool_calls import ProjectTaskToolCallRow
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.repos.project_tasks import ProjectTaskRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import OctopError
from octop.infra.projects import runtime, tasks
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path):
    db = SqlitePool(tmp_path / "deletion.sqlite")
    run_migrations(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(2,'other','user',1)")
    bundle = SharedServices(PathLayout(tmp_path), OctopConfig(), RepoBundle.from_pool(db))
    bundle.secret_repo.get_or_create("jwt", lambda: b"deletion-test-only-signing-key")
    try:
        yield bundle
    finally:
        db.close()


def queued(services, task_id):
    now = tasks.now_ms()
    turn = ProjectTaskTurnRow(
        f"turn-{task_id}",
        task_id,
        f"request-{task_id}",
        "Work",
        "",
        "queued",
        "",
        "",
        '{"action_timeline_enabled":false}',
        "unused/model",
        "",
        False,
        now,
        now,
    )
    with services.db.transaction() as conn:
        services.project_task_repo.insert(
            conn,
            ProjectTaskRow(task_id, 1, None, "Work", f"create-{task_id}", turn.turn_id, now, now),
        )
        services.project_task_turn_repo.insert(conn, turn)
    return turn


def running(services, task_id):
    turn = queued(services, task_id)
    claimed = services.project_task_turn_repo.claim(f"worker-{task_id}", tasks.now_ms())
    assert claimed.turn_id == turn.turn_id
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET pause_requested=1 WHERE turn_id=?", (turn.turn_id,)
        )
    return replace(claimed, pause_requested=True)


async def test_waits_for_historical_execution_cleanup_without_stopping_another_task(services):
    selected = queued(services, "selected")
    tasks.cancel(services, 1, selected.task_id, selected.turn_id)
    other = queued(services, "other")
    entered, cleaning, release, other_cancelled = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def finishing():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    async def unrelated():
        try:
            await asyncio.Event().wait()
        finally:
            other_cancelled.set()

    worker = runtime.ProjectTaskWorker(services)
    execution = asyncio.create_task(finishing())
    other_execution = asyncio.create_task(unrelated())
    worker._running[selected.turn_id] = execution
    worker._running[other.turn_id] = other_execution
    await entered.wait()
    stopped = asyncio.create_task(worker.stop_for_deletion(1, [selected.task_id], timeout=1))
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        assert services.project_task_repo.get(selected.task_id, 1).active_turn_id is None
        assert not stopped.done() and not other_cancelled.is_set()
        release.set()
        assert await stopped is True
        assert execution.done() and not other_execution.done()
        assert services.project_task_turn_repo.get(other.turn_id).status == "queued"
    finally:
        release.set()
        await worker.close()
        await asyncio.gather(stopped, execution, other_execution, return_exceptions=True)


async def test_timeout_and_repeated_observation_do_not_cancel_cleanup_again(services):
    turn = queued(services, "slow-cleanup")
    tasks.cancel(services, 1, turn.task_id, turn.turn_id)
    entered, cleaning, release, cancelled_again = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def finishing():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled_again.set()
                raise

    worker = runtime.ProjectTaskWorker(services)
    execution = asyncio.create_task(finishing())
    worker._running[turn.turn_id] = execution
    await entered.wait()
    try:
        assert await worker.stop_for_deletion(1, [turn.task_id], timeout=0.02) is False
        assert cleaning.is_set() and not execution.done()
        assert await worker.stop_for_deletion(1, [turn.task_id], timeout=0.02) is False
        assert not cancelled_again.is_set() and not execution.done()
        release.set()
        await asyncio.gather(execution, return_exceptions=True)
        assert await worker.stop_for_deletion(1, [turn.task_id]) is True
    finally:
        release.set()
        await asyncio.gather(execution, return_exceptions=True)
        await worker.close()


async def test_real_execution_heartbeat_does_not_cancel_slow_cleanup_again(services, monkeypatch):
    turn = running(services, "heartbeat-cleanup")
    # Start with an ordinary running turn. The deletion observer owns the first
    # cancellation; a pre-existing pause could let the heartbeat cancel first.
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE project_task_turns SET pause_requested=0 WHERE turn_id=?", (turn.turn_id,)
        )
    turn = replace(turn, pause_requested=False)
    entered, cleaning, heartbeat_observed, release, cleaned, cancelled_again = (
        asyncio.Event() for _ in range(6)
    )
    loop = asyncio.get_running_loop()
    cleanup_entered = ThreadEvent()
    original_progress = services.project_task_turn_repo.progress

    def progress(*args, **kwargs):
        stopped = original_progress(*args, **kwargs)
        if stopped:
            # progress has already released its DB transaction. Hold this
            # heartbeat result until the real execution enters slow cleanup,
            # so the test always exercises the second-cancel guard.
            cleanup_entered.wait()
            loop.call_soon_threadsafe(heartbeat_observed.set)
        return stopped

    async def run(*_args):
        try:
            yield {"type": "token", "content": "saved partial reply"}
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            cleanup_entered.set()
            try:
                await release.wait()
                cleaned.set()
            except asyncio.CancelledError:
                cancelled_again.set()
                raise

    monkeypatch.setattr(runtime, "_OUTPUT_SAVE_SECONDS", 0.005)
    monkeypatch.setattr(runtime, "_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(runtime, "run_turn", run)
    monkeypatch.setattr(services.project_task_turn_repo, "progress", progress)
    worker = runtime.ProjectTaskWorker(services)
    execution = asyncio.create_task(worker._execute(turn))
    worker._running[turn.turn_id] = execution
    observation = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        observation = asyncio.create_task(worker.stop_for_deletion(1, [turn.task_id], timeout=0.08))
        await asyncio.wait_for(cleaning.wait(), 1)
        await asyncio.wait_for(heartbeat_observed.wait(), 1)
        assert await observation is False
        assert not execution.done() and not cancelled_again.is_set()
        assert not cleaned.is_set()
        assert services.project_task_turn_repo.get(turn.turn_id).status == "running"
        assert services.project_task_repo.get(turn.task_id, 1).active_turn_id == turn.turn_id
        release.set()
        assert await worker.stop_for_deletion(1, [turn.task_id], timeout=1) is True
        assert execution.done() and cleaned.is_set() and not cancelled_again.is_set()
        saved = services.project_task_turn_repo.get(turn.turn_id)
        assert saved.status == "cancelled" and saved.reply == "saved partial reply"
        assert services.project_task_repo.get(turn.task_id, 1).active_turn_id is None
    finally:
        cleanup_entered.set()
        release.set()
        await asyncio.gather(execution, return_exceptions=True)
        if observation is not None:
            await asyncio.gather(observation, return_exceptions=True)
        await worker.close()


@pytest.mark.parametrize("dispatch", ["client_claimed", "dispatched", "unknown"])
async def test_dispatched_external_action_must_resolve_before_cancellation(services, dispatch):
    turn = running(services, "external")
    now = tasks.now_ms()
    with services.db.transaction() as conn:
        if dispatch == "client_claimed":
            services.project_task_tool_call_repo.insert(
                conn,
                ProjectTaskToolCallRow(
                    "call-external",
                    turn.task_id,
                    turn.turn_id,
                    1,
                    "client",
                    "exec_command",
                    "{}",
                    "pending",
                    "",
                    "",
                    "",
                    "",
                    now,
                    now,
                    now + 60_000,
                ),
            )
            services.project_task_tool_call_repo.claim(
                conn, "call-external", "claim-external", "claim-token", now
            )
        else:
            services.project_task_execution_receipt_repo.start(
                conn,
                "receipt-external",
                turn.task_id,
                turn.turn_id,
                "call-external",
                "execute",
                "digest",
                turn.worker_id,
                now,
            )
            if dispatch == "unknown":
                services.project_task_execution_receipt_repo.unknown(
                    conn, turn.turn_id, "call-external", turn.worker_id, now
                )
    entered, cancelled = asyncio.Event(), asyncio.Event()
    worker = runtime.ProjectTaskWorker(services)

    async def executing():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await asyncio.to_thread(worker._finish, turn, "cancelled", "saved reply", "")

    execution = asyncio.create_task(executing())
    worker._running[turn.turn_id] = execution
    await entered.wait()
    try:
        assert await worker.stop_for_deletion(1, [turn.task_id], timeout=0.02) is False
        assert not cancelled.is_set()
        assert not services.project_task_turn_repo.get(turn.turn_id).cancel_requested
        with services.db.transaction() as conn:
            if dispatch == "client_claimed":
                services.project_task_tool_call_repo.complete(conn, "call-external", "{}", now)
            else:
                # An authoritative reconciliation result, never an inferred timeout.
                conn.execute(
                    "UPDATE project_task_execution_receipts SET state='completed' WHERE turn_id=?",
                    (turn.turn_id,),
                )
        assert await worker.stop_for_deletion(1, [turn.task_id], timeout=1) is True
        assert cancelled.is_set() and execution.done()
        assert services.project_task_repo.get(turn.task_id, 1).active_turn_id is None
        assert services.project_task_turn_repo.get(turn.turn_id).reply == "saved reply"
    finally:
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        await worker.close()


async def test_foreign_worker_lease_requires_real_finalization(services):
    turn = running(services, "foreign-process")
    worker = runtime.ProjectTaskWorker(services)
    assert await worker.stop_for_deletion(1, [turn.task_id], timeout=0.02) is False
    current = services.project_task_turn_repo.get(turn.turn_id)
    assert current.status == "running" and current.cancel_requested
    assert current.worker_id == turn.worker_id
    assert services.project_task_repo.get(turn.task_id, 1).active_turn_id == turn.turn_id
    await asyncio.to_thread(worker._finish, current, "cancelled", "saved reply", "")
    assert await worker.stop_for_deletion(1, [turn.task_id]) is True


async def test_queued_turn_stops_and_foreign_owner_cannot_cancel(services):
    turn = queued(services, "queued")
    worker = runtime.ProjectTaskWorker(services)
    with pytest.raises(OctopError):
        await worker.stop_for_deletion(2, [turn.task_id])
    assert not services.project_task_turn_repo.get(turn.turn_id).cancel_requested
    assert await worker.stop_for_deletion(1, [turn.task_id]) is True
    assert services.project_task_turn_repo.get(turn.turn_id).status == "cancelled"
    assert services.project_task_repo.get(turn.task_id, 1).active_turn_id is None


async def test_cancel_during_claim_handoff_does_not_start_graph_or_leave_lease(
    services, monkeypatch
):
    turn = queued(services, "claim-handoff")
    original_claim = services.project_task_turn_repo.claim
    invoked = []

    def claim(worker_id, now):
        claimed = original_claim(worker_id, now)
        if claimed is not None:
            tasks.cancel(services, 1, claimed.task_id, claimed.turn_id)
        return claimed

    async def run(*args):
        invoked.append(args)
        yield {"type": "token", "content": "must not execute"}

    monkeypatch.setattr(services.project_task_turn_repo, "claim", claim)
    monkeypatch.setattr(runtime, "run_turn", run)
    worker = runtime.ProjectTaskWorker(services)
    worker._loop_task = asyncio.create_task(worker._loop())
    try:
        async with asyncio.timeout(2):
            while services.project_task_turn_repo.get(turn.turn_id).status != "cancelled":
                await asyncio.sleep(0.01)
        assert not invoked
        assert services.project_task_repo.get(turn.task_id, 1).active_turn_id is None
        assert not worker._running
    finally:
        await worker.close()
