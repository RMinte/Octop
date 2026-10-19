"""Real graph checkpoint writes cannot outlive their owning worker lease."""

import asyncio
import sys
import textwrap
import threading
from typing import TypedDict

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph

from octop.config import OctopConfig
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.project_task_turns import ProjectTaskTurnRow
from octop.infra.db.services import RepoBundle, SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects.checkpoint_saver import TaskCheckpointSaver, checkpoint_fence
from octop.infra.utils.paths import PathLayout


class State(TypedDict):
    count: int


@pytest.fixture
def checkpoint_env(tmp_path):
    db = SqlitePool(tmp_path / "task-leases.db")
    run_migrations(db)
    repos = RepoBundle.from_pool(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users(id,username,role,created_at) VALUES(1,'owner','user',1)")
        conn.execute(
            "INSERT INTO project_tasks(task_id,owner_id,title,request_id,active_turn_id,"
            "created_at,updated_at) VALUES('task-one',1,'Keep progress','request-one',"
            "'turn-one',100,100)"
        )
        repos.project_task_turn_repo.insert(
            conn,
            ProjectTaskTurnRow(
                turn_id="turn-one",
                task_id="task-one",
                request_id="turn-request",
                prompt="Continue progress",
                reply="",
                status="queued",
                error_code="",
                activity="",
                configuration_json="{}",
                model_ref="provider/model",
                worker_id="",
                cancel_requested=False,
                created_at=100,
                updated_at=100,
            ),
        )
    turn = repos.project_task_turn_repo.claim("worker-one", 1_000)
    services = SharedServices(PathLayout(tmp_path), OctopConfig(), repos)
    try:
        yield services, turn, tmp_path / "native-checkpoints.db"
    finally:
        db.close()


def replace_worker(services, checkpoint_id):
    turn = services.project_task_turn_repo.get("turn-one")
    with checkpoint_fence(services, turn), services.db.transaction() as conn:
        assert services.project_task_repo.lock_owner(conn, 1)
        assert services.project_task_turn_repo.park_recovery(
            conn, "turn-one", "worker-one", checkpoint_id, '{"mode":"graph"}', 2_000
        )
        assert services.project_task_turn_repo.resume_recovery(conn, "turn-one", 3_000)
    return services.project_task_turn_repo.claim("worker-two", 4_000)


def graph_for(node, saver):
    builder = StateGraph(State)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return builder.compile(checkpointer=saver)


async def test_old_graph_cannot_write_after_new_lease_and_new_graph_resumes_same_thread(
    checkpoint_env,
):
    services, original, path = checkpoint_env
    entered, release = asyncio.Event(), asyncio.Event()

    async def work(state):
        entered.set()
        await release.wait()
        return {"count": state["count"] + 1}

    config = {"configurable": {"thread_id": original.turn_id}}
    async with AsyncSqliteSaver.from_conn_string(str(path)) as delegate:
        old_saver = TaskCheckpointSaver(services, 1, original, delegate)
        old_graph = graph_for(work, old_saver)
        pending = asyncio.create_task(old_graph.ainvoke({"count": 0}, config, durability="sync"))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            before = await delegate.aget_tuple(config)
            assert before is not None
            replacement = await asyncio.to_thread(
                replace_worker, services, before.config["configurable"]["checkpoint_id"]
            )
            release.set()
            with pytest.raises(OctopError) as rejected:
                await asyncio.wait_for(pending, 3)
            assert rejected.value.code == ErrorCode.TASK_INTERRUPTED
            after = await delegate.aget_tuple(config)
            assert after.config == before.config
            assert after.checkpoint == before.checkpoint
            assert after.pending_writes == before.pending_writes

            # Both checkpoint writes and intermediate node writes reject the old lease.
            with pytest.raises(OctopError):
                await old_saver.aput_writes(before.config, [("count", 99)], "late-node")
            with pytest.raises(OctopError):
                await old_saver.aput(before.config, before.checkpoint, before.metadata, {})

            saver = TaskCheckpointSaver(services, 1, replacement, delegate)
            graph = graph_for(work, saver)
            assert await asyncio.wait_for(graph.ainvoke(None, config, durability="sync"), 3) == {
                "count": 1
            }
            final = await saver.aget_tuple(config)
            assert final.config["configurable"]["thread_id"] == original.turn_id
            assert final.checkpoint["channel_values"]["count"] == 1
            assert final.config != before.config
            assert saver.serde is delegate.serde and saver.config_specs == delegate.config_specs
            assert len([item async for item in saver.alist(config, limit=1)]) == 1
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


class PausedSqliteSaver(AsyncSqliteSaver):
    """Delay an actual SQLite write so worker replacement can contend with it."""

    def __init__(self, conn):
        super().__init__(conn)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.saved = threading.Event()

    async def aput(self, config, checkpoint, metadata, new_versions):
        self.entered.set()
        await self.release.wait()
        result = await super().aput(config, checkpoint, metadata, new_versions)
        self.saved.set()
        return result


@pytest.mark.parametrize("cancel", [False, True])
async def test_replacement_waits_for_admitted_checkpoint_write_to_drain(checkpoint_env, cancel):
    services, original, path = checkpoint_env
    attempted, replaced = threading.Event(), threading.Event()

    async def work(state):
        return {"count": state["count"] + 1}

    config = {"configurable": {"thread_id": original.turn_id}}
    async with PausedSqliteSaver.from_conn_string(str(path)) as delegate:
        saver = TaskCheckpointSaver(services, 1, original, delegate)
        graph = graph_for(work, saver)
        pending = asyncio.create_task(graph.ainvoke({"count": 0}, config, durability="sync"))
        replacement = None
        try:
            await asyncio.wait_for(delegate.entered.wait(), 3)

            def reclaim():
                attempted.set()
                with checkpoint_fence(services, original), services.db.transaction() as conn:
                    assert services.project_task_repo.lock_owner(conn, 1)
                    assert delegate.saved.is_set(), (
                        "A lease changed before its admitted write ended"
                    )
                    conn.execute(
                        "UPDATE project_task_turns SET worker_id='worker-two' WHERE turn_id='turn-one'"
                    )
                    replaced.set()

            replacement = asyncio.create_task(asyncio.to_thread(reclaim))
            assert await asyncio.to_thread(attempted.wait, 2)
            if cancel:
                pending.cancel()
            await asyncio.sleep(0.02)
            assert not replaced.is_set()
            assert not pending.done()
            # JWT/auth still performs synchronous reads on this loop. An admitted
            # native write must leave the main DB lock available to those reads.
            with services.db.connect() as conn:
                assert (
                    conn.execute("SELECT username FROM users WHERE id=1").fetchone()[0] == "owner"
                )
            delegate.release.set()
            await asyncio.wait_for(replacement, 3)
            if cancel:
                # LangGraph's finally may try another checkpoint after cancellation;
                # that write must be fenced once reclamation has changed the lease.
                with pytest.raises((asyncio.CancelledError, OctopError)):
                    await asyncio.wait_for(pending, 3)
            else:
                # The graph may finish before reclamation or be fenced on its next write.
                result = await asyncio.gather(pending, return_exceptions=True)
                assert result[0] == {"count": 1} or isinstance(result[0], OctopError)
            assert replaced.is_set()
            assert await delegate.aget_tuple(config) is not None
        finally:
            delegate.release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if replacement is not None:
                await asyncio.gather(replacement, return_exceptions=True)


async def test_sync_checkpoint_writes_are_explicitly_unavailable(checkpoint_env):
    services, original, path = checkpoint_env
    async with AsyncSqliteSaver.from_conn_string(str(path)) as delegate:
        saver = TaskCheckpointSaver(services, 1, original, delegate)
        with pytest.raises(NotImplementedError, match="asynchronous"):
            saver.put_writes({}, [], "node")


async def test_checkpoint_fence_blocks_another_process_and_releases_on_crash(checkpoint_env):
    services, _original, _path = checkpoint_env
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from octop.infra.projects.checkpoint_saver import checkpoint_fence

        services = SimpleNamespace(paths=SimpleNamespace(root=Path(sys.argv[1])))
        turn = SimpleNamespace(task_id="task-one", turn_id="turn-one")
        if sys.argv[2] == "wait":
            print("waiting", flush=True)
        with checkpoint_fence(services, turn):
            print("locked", flush=True)
            if sys.argv[2] == "hold":
                sys.stdin.buffer.read(1)
        """
    )
    processes = []
    try:
        first = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(services.paths.root),
            "hold",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(first)
        assert (await asyncio.wait_for(first.stdout.readline(), 5)).strip() == b"locked"
        second = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            str(services.paths.root),
            "wait",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        processes.append(second)
        assert (await asyncio.wait_for(second.stdout.readline(), 5)).strip() == b"waiting"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second.stdout.readline(), 0.05)
        first.kill()
        await asyncio.wait_for(first.wait(), 3)
        assert (await asyncio.wait_for(second.stdout.readline(), 3)).strip() == b"locked"
        assert await asyncio.wait_for(second.wait(), 3) == 0
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
        await asyncio.gather(*(process.wait() for process in processes))
