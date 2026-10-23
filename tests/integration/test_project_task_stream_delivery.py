"""Committed output wakes task streams without accelerating unrelated polling."""

from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from octop.api.routers import project_tasks_ws
from octop.infra.projects import runtime, tasks
from tests.integration.test_project_tasks import settled
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.support.auth import resolve_user_id


@pytest.fixture
async def delivery_env(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    streams = {}
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 3600)

    async def run(services, owner, turn, history):
        queue = streams.setdefault(turn.prompt, asyncio.Queue())
        while (event := await queue.get()) is not None:
            yield event

    monkeypatch.setattr(runtime, "run_turn", run)
    yield client, server, auth, streams
    for queue in streams.values():
        queue.put_nowait(None)


async def create_task(client, auth, prompt):
    response = await client.post(
        "/api/project-tasks",
        headers=auth,
        json={
            "request_id": "delivery-" + prompt,
            "prompt": prompt,
            "options": {"action_timeline_enabled": False},
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def next_turn(ws, task_id, *, reply=None, reasoning=None, status="running"):
    def matches(frame):
        if frame["type"] != "turn" or frame["data"]["task"]["task_id"] != task_id:
            return False
        turn = frame["data"]["turn"]
        return (
            turn["status"] == status
            and (reply is None or turn["reply"] == reply)
            and (reasoning is None or turn["reasoning"] == reasoning)
        )

    return (await until(ws, matches))["data"]["turn"]


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_live_batches_broadcast_without_polling_and_release_watches(
    delivery_env, record_property
):
    client, server, auth, streams = delivery_env
    queue = streams["batches"] = asyncio.Queue()
    task_id = (await create_task(client, auth, "batches"))["task_id"]
    worker = server.project_task_worker
    session_a, ws_a = await connected(client, auth)
    session_b, ws_b = await connected(client, auth)
    try:
        for ws, key in ((ws_a, "watch-batches-a"), (ws_b, "watch-batches-b")):
            assert (await command(ws, "task.subscribe", key, task_id=task_id))["type"] == "result"
        assert len(worker._update_listeners[task_id]) == 2
        await command(ws_a, "task.subscribe", "watch-batches-again", task_id=task_id)
        assert len(worker._update_listeners[task_id]) == 2

        reply, reasoning, received = "", "", []
        for kind, text in (
            ("reasoning", "先"),
            ("reasoning", "检查。"),
            ("token", "这是"),
            ("token", "持续"),
            ("token", "回答。"),
        ):
            queue.put_nowait({"type": kind, "content": text})
            if kind == "token":
                reply += text
            else:
                reasoning += text
            turns = await asyncio.gather(
                next_turn(ws_a, task_id, reply=reply, reasoning=reasoning),
                next_turn(ws_b, task_id, reply=reply, reasoning=reasoning),
            )
            assert turns[0]["content_revision"] == turns[1]["content_revision"]
            received.append(time.monotonic())
        record_property(
            "delivery_gaps_ms",
            [
                round((later - earlier) * 1000, 1)
                for earlier, later in zip(received, received[1:], strict=False)
            ],
        )

        await command(ws_b, "task.unsubscribe", "unwatch-batches-b", task_id=task_id)
        assert len(worker._update_listeners[task_id]) == 1
        queue.put_nowait(None)
        completed = await next_turn(
            ws_a, task_id, reply=reply, reasoning=reasoning, status="completed"
        )
        assert completed["finished_at"] is not None
    finally:
        await session_a.__aexit__(None, None, None)
        await session_b.__aexit__(None, None, None)
    assert task_id not in worker._update_listeners


async def test_output_notifications_only_read_the_changed_task(delivery_env, monkeypatch):
    client, server, auth, streams = delivery_env
    static_queue = streams["static"] = asyncio.Queue()
    static_queue.put_nowait({"type": "token", "content": "静态正文"})
    static_queue.put_nowait(None)
    static_id = (await create_task(client, auth, "static"))["task_id"]
    assert (await settled(client, auth, static_id))["status"] == "completed"
    active_queue = streams["active"] = asyncio.Queue()
    active_id = (await create_task(client, auth, "active"))["task_id"]

    reads = Counter()
    original_updates = tasks.updates

    def updates(services, owner, task_id, cursor):
        reads[task_id] += 1
        return original_updates(services, owner, task_id, cursor)

    monkeypatch.setattr(tasks, "updates", updates)
    policy = Mock(wraps=project_tasks_ws.approval_policy.current)
    tools = Mock(wraps=project_tasks_ws.client_tools.open_calls)
    monkeypatch.setattr(project_tasks_ws.approval_policy, "current", policy)
    monkeypatch.setattr(project_tasks_ws.client_tools, "open_calls", tools)
    session, ws = await connected(client, auth)
    try:
        await command(ws, "client.attach", "attach-delivery-client", client_id="delivery-client")
        for task_id in (static_id, active_id):
            await command(ws, "task.subscribe", "subscribe-" + task_id, task_id=task_id)
        # Each subscription reads once in the command and once after installing its watch.
        await wait_for(lambda: reads[static_id] >= 2 and reads[active_id] >= 2)
        baseline = reads[static_id], policy.call_count, tools.call_count
        reply = ""
        for text in ("连", "续", "正", "文"):
            active_queue.put_nowait({"type": "token", "content": text})
            reply += text
            await next_turn(ws, active_id, reply=reply)
        assert (reads[static_id], policy.call_count, tools.call_count) == baseline
        assert reads[active_id] >= 6
    finally:
        await session.__aexit__(None, None, None)
    assert server.project_task_worker._update_listeners == {}


async def test_idle_output_keeps_the_lease_write_interval(delivery_env, monkeypatch):
    client, server, auth, streams = delivery_env
    queue = streams["idle"] = asyncio.Queue()
    current_time = 100.0
    clock_reads = asyncio.Queue()

    def monotonic():
        clock_reads.put_nowait(current_time)
        return current_time

    # Replace only the runtime's clock, preserving the event loop's real timeout clock.
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(runtime, "_OUTPUT_SAVE_SECONDS", 0.01)
    task_id = (await create_task(client, auth, "idle"))["task_id"]
    session, ws = await connected(client, auth)
    try:
        await command(ws, "task.subscribe", "subscribe-idle-turn", task_id=task_id)
        queue.put_nowait({"type": "token", "content": "已保存首段"})
        await next_turn(ws, task_id, reply="已保存首段")
        progress = Mock(wraps=server.services.project_task_turn_repo.progress)
        notify = Mock(wraps=server.project_task_worker._notify_update)
        monkeypatch.setattr(server.services.project_task_turn_repo, "progress", progress)
        monkeypatch.setattr(server.project_task_worker, "_notify_update", notify)

        for elapsed in (0.1, 0.2, 0.3, 0.49):
            while not clock_reads.empty():
                clock_reads.get_nowait()
            current_time = 100.0 + elapsed
            assert await asyncio.wait_for(clock_reads.get(), 5) == current_time
            assert progress.call_count == 0

        while not clock_reads.empty():
            clock_reads.get_nowait()
        current_time = 100.5
        # The first read checks the lease deadline; the second follows the committed save.
        assert await asyncio.wait_for(clock_reads.get(), 5) == current_time
        assert await asyncio.wait_for(clock_reads.get(), 5) == current_time
        assert progress.call_count == 1
        assert notify.call_count == 0
    finally:
        queue.put_nowait(None)
        await settled(client, auth, task_id)
        await session.__aexit__(None, None, None)


async def external_turn(client, server, auth):
    # A closed local worker models a write from another process, without emitting local notices.
    await server.project_task_worker.close()
    services = server.services
    owner = await resolve_user_id(client, auth, "admin")
    created = await asyncio.to_thread(
        tasks.send,
        services,
        owner,
        tasks.TaskMessage(
            request_id="external-delivery-turn",
            prompt="External delivery",
            options={"action_timeline_enabled": False},
        ),
    )
    claimed = await asyncio.to_thread(
        services.project_task_turn_repo.claim, "external-delivery-worker", tasks.now_ms()
    )
    assert claimed is not None and claimed.task_id == created.task_id
    return claimed


async def write_reply(server, turn, reply):
    assert not await asyncio.to_thread(
        server.services.project_task_turn_repo.progress,
        turn.turn_id,
        turn.worker_id,
        reply,
        "",
        tasks.now_ms(),
    )


async def test_fallback_delivers_commits_without_a_local_notification(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.02)
    turn = await external_turn(client, server, auth)
    session, ws = await connected(client, auth)
    try:
        await command(ws, "task.subscribe", "subscribe-external-turn", task_id=turn.task_id)
        await write_reply(server, turn, "其他进程保存的正文")
        await next_turn(ws, turn.task_id, reply="其他进程保存的正文")
    finally:
        await session.__aexit__(None, None, None)
    assert server.project_task_worker._update_listeners == {}


async def test_notification_during_a_read_is_retained_for_the_next_read(
    env_with_provider, monkeypatch
):
    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 3600)
    turn = await external_turn(client, server, auth)
    original_updates = tasks.updates
    read_started, release_read = threading.Event(), threading.Event()
    reads = 0
    intercept = False

    def updates(*args):
        nonlocal reads, intercept
        result = original_updates(*args)
        reads += 1
        if intercept:
            intercept = False
            read_started.set()
            assert release_read.wait(5), "test did not release the first database read"
        return result

    monkeypatch.setattr(tasks, "updates", updates)
    session, ws = await connected(client, auth)
    try:
        await command(ws, "task.subscribe", "subscribe-race-turn", task_id=turn.task_id)
        await wait_for(lambda: reads >= 2)
        await write_reply(server, turn, "第一批")
        intercept = True
        server.project_task_worker._notify_update(turn.task_id)
        assert await asyncio.to_thread(read_started.wait, 5)
        await write_reply(server, turn, "第一批，第二批")
        server.project_task_worker._notify_update(turn.task_id)
        release_read.set()
        await next_turn(ws, turn.task_id, reply="第一批")
        await next_turn(ws, turn.task_id, reply="第一批，第二批")
    finally:
        release_read.set()
        await session.__aexit__(None, None, None)
    assert server.project_task_worker._update_listeners == {}
