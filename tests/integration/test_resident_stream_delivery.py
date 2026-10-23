"""Resident receipts and task turns share one durable WebSocket delivery path."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from uuid import uuid4

import pytest

from octop.api.routers import project_tasks_ws
from octop.infra.projects import runtime as task_runtime
from tests.integration.test_project_tasks_ws import command, connected, until
from tests.integration.test_resident_conversations import _base, _binding, _turn
from tests.support.auth import create_user, resolve_user_id
from tests.support.harness import patch_resident_stream, patch_task_stream


@pytest.fixture
async def resident_delivery(env_with_provider, monkeypatch):
    client, server, auth = env_with_provider
    binding = await _binding(client, auth)
    resident = server.app_runtime.resident_conversations
    streams, invocations, cancelled = {}, [], set()
    watches = Counter()
    original_watch = resident.watch_updates

    def watch(thread_id, wake):
        stop = original_watch(thread_id, wake)
        watches[thread_id] += 1
        active = True

        def unsubscribe():
            nonlocal active
            if active:
                active = False
                watches[thread_id] -= 1
                stop()

        return unsubscribe

    async def stream(message):
        invocations.append(message)
        queue = streams.setdefault(message.text, asyncio.Queue())
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
            yield {"type": "done"}
        except asyncio.CancelledError:
            cancelled.add(message.metadata["_resident_request_id"])
            raise

    monkeypatch.setattr(resident, "watch_updates", watch)
    patch_resident_stream(monkeypatch, stream)
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 3600)
    yield SimpleNamespace(
        client=client,
        server=server,
        auth=auth,
        binding=binding,
        base=_base(binding),
        resident=resident,
        streams=streams,
        invocations=invocations,
        cancelled=cancelled,
        watches=watches,
    )
    for queue in streams.values():
        queue.put_nowait(None)


async def send(env, prompt):
    queue = env.streams[prompt] = asyncio.Queue()
    request_id = uuid4().hex
    response = await env.client.post(
        env.base + "/messages",
        headers=env.auth,
        json={"request_id": request_id, "prompt": prompt},
    )
    assert response.status_code == 202, response.text
    return response.json(), queue


async def subscribe(ws, binding, key):
    await ws.send_json(
        {
            "type": "resident.subscribe",
            "id": key,
            "agent_id": binding["agent_id"],
            "thread_id": binding["thread_id"],
        }
    )
    first = await until(
        ws,
        lambda frame: (
            frame["type"] == "resident.snapshot"
            and frame["data"]["thread_id"] == binding["thread_id"]
        ),
    )
    # Consuming the snapshot first also verifies that result is not sent ahead of it.
    assert await until(ws, lambda frame: frame.get("id") == key) == {
        "type": "result",
        "id": key,
        "data": None,
    }
    return first["data"]


async def next_snapshot(ws, binding, turn_id, *, reply, status="running"):
    def matches(frame):
        return (
            frame["type"] == "resident.snapshot"
            and frame["data"]["thread_id"] == binding["thread_id"]
            and any(
                turn["turn_id"] == turn_id and turn["reply"] == reply and turn["status"] == status
                for turn in frame["data"]["turns"]
            )
        )

    return (await until(ws, matches))["data"]


async def receipt(env, request_id, reply):
    async with asyncio.timeout(5):
        while True:
            response = await env.client.get(env.base + "/requests/" + request_id, headers=env.auth)
            assert response.status_code == 200, response.text
            if response.json()["reply"] == reply:
                return response.json()
            await asyncio.sleep(0.01)


async def test_resident_stream_publishes_committed_batches_and_terminal_without_polling(
    resident_delivery,
):
    env = resident_delivery
    capability = await env.client.get("/api/agents/resident-capabilities", headers=env.auth)
    assert capability.status_code == 200, capability.text
    assert capability.json()["conversation_transport"] == "rest_polling"
    assert capability.json()["conversation_stream"] == {
        "transport": "websocket",
        "path": "/api/project-tasks/ws",
        "protocol_version": 3,
    }
    session, ws = await connected(env.client, env.auth)
    try:
        initial = await subscribe(ws, env.binding, "resident-first")
        assert initial["turns"] == [] and initial["active_turn_id"] is None
        assert len(env.invocations) == 0
        await subscribe(ws, env.binding, "resident-replace")
        assert env.watches[env.binding["thread_id"]] == 1
        accepted, queue = await send(env, "Stream a durable resident answer.")
        reply, revisions, turn_revisions = "", [initial["revision"]], [accepted["revision"]]
        for text in ("首段", "，第二段", "，第三段。"):
            queue.put_nowait({"type": "token", "content": text})
            reply += text
            snapshot = await next_snapshot(ws, env.binding, accepted["turn_id"], reply=reply)
            current = snapshot["turns"][-1]
            assert snapshot["active_turn_id"] == accepted["turn_id"]
            rest = await receipt(env, accepted["request_id"], reply)
            assert rest == current
            history = await env.client.get(env.base + "/conversation/history", headers=env.auth)
            assert history.status_code == 200, history.text
            saved = next(
                item["turn"]
                for item in history.json()["items"]
                if item.get("turn", {}).get("turn_id") == accepted["turn_id"]
            )
            assert saved == current
            revisions.append(snapshot["revision"])
            turn_revisions.append(current["revision"])
        queue.put_nowait(None)
        final = await next_snapshot(
            ws, env.binding, accepted["turn_id"], reply=reply, status="completed"
        )
        assert final["active_turn_id"] is None
        assert final["turns"][-1]["revision"] > turn_revisions[-1]
        assert final["revision"] > revisions[-1]
        assert all(b > a for a, b in zip(revisions, revisions[1:], strict=False))
        assert all(b > a for a, b in zip(turn_revisions, turn_revisions[1:], strict=False))
        assert len(env.invocations) == 1
        assert (
            await _turn(env.client, env.auth, env.base, accepted["request_id"], "completed")
        ) == final["turns"][-1]
        await command(
            ws,
            "resident.unsubscribe",
            "resident-stop",
            agent_id=env.binding["agent_id"],
            thread_id=env.binding["thread_id"],
        )
        assert env.watches[env.binding["thread_id"]] == 0
    finally:
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values())


async def test_one_socket_keeps_task_and_resident_updates_independent(
    resident_delivery, monkeypatch
):
    env = resident_delivery
    task_queue = asyncio.Queue()

    async def task_stream(services, owner, turn, history):
        while (chunk := await task_queue.get()) is not None:
            yield chunk

    patch_task_stream(monkeypatch, task_stream)
    session, ws = await connected(env.client, env.auth)
    try:
        await subscribe(ws, env.binding, "shared-resident")
        created = await env.client.post(
            "/api/project-tasks",
            headers=env.auth,
            json={
                "request_id": uuid4().hex,
                "prompt": "Independent task output.",
                "options": {"action_timeline_enabled": False},
            },
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        await command(ws, "task.subscribe", "shared-task", task_id=task_id)
        accepted, queue = await send(env, "Independent resident output.")
        queue.put_nowait({"type": "token", "content": "Resident body"})
        resident = await next_snapshot(ws, env.binding, accepted["turn_id"], reply="Resident body")
        assert resident["agent_id"] == env.binding["agent_id"]
        task_queue.put_nowait({"type": "token", "content": "Task body"})
        turn = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn"
                and frame["data"]["task"]["task_id"] == task_id
                and frame["data"]["turn"]["reply"] == "Task body"
            ),
        )
        assert turn["data"]["turn"]["status"] == "running"
        await command(ws, "task.unsubscribe", "stop-shared-task", task_id=task_id)
        queue.put_nowait({"type": "token", "content": " keeps streaming"})
        await next_snapshot(
            ws, env.binding, accepted["turn_id"], reply="Resident body keeps streaming"
        )
        queue.put_nowait(None)
        await next_snapshot(
            ws,
            env.binding,
            accepted["turn_id"],
            reply="Resident body keeps streaming",
            status="completed",
        )
    finally:
        task_queue.put_nowait(None)
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values())


async def test_disconnect_keeps_execution_and_reconnect_sends_latest_snapshot(resident_delivery):
    env = resident_delivery
    session, ws = await connected(env.client, env.auth)
    accepted, queue = await send(env, "Continue through disconnect.")
    try:
        await subscribe(ws, env.binding, "before-disconnect")
        queue.put_nowait({"type": "token", "content": "Saved first"})
        first = await next_snapshot(ws, env.binding, accepted["turn_id"], reply="Saved first")
    finally:
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values()) and not env.cancelled
    queue.put_nowait({"type": "token", "content": " and second"})
    await receipt(env, accepted["request_id"], "Saved first and second")
    resumed_session, resumed_ws = await connected(env.client, env.auth)
    try:
        resumed = await subscribe(resumed_ws, env.binding, "after-disconnect")
        assert resumed["turns"][-1]["reply"] == "Saved first and second"
        assert resumed["turns"][-1]["status"] == "running"
        assert resumed["revision"] > first["revision"]
        assert len(env.invocations) == 1 and not env.cancelled
        queue.put_nowait(None)
        await next_snapshot(
            resumed_ws,
            env.binding,
            accepted["turn_id"],
            reply="Saved first and second",
            status="completed",
        )
    finally:
        await resumed_session.__aexit__(None, None, None)
    assert not any(env.watches.values())


async def test_resident_subscription_checks_owner_without_dropping_valid_subscriptions(
    resident_delivery,
):
    env = resident_delivery
    other_auth = await create_user(env.client, env.auth, username="resident-stream-other")
    foreign = await _binding(env.client, other_auth)
    session, ws = await connected(env.client, other_auth)
    try:
        denied = await command(
            ws,
            "resident.subscribe",
            "foreign-resident",
            agent_id=env.binding["agent_id"],
            thread_id=env.binding["thread_id"],
        )
        assert denied["type"] == "error" and denied["status"] == 404
        assert denied["error"]["code"] == "NOT_FOUND"
        assert not any(env.watches.values())
        owned = await subscribe(ws, foreign, "own-resident")
        assert owned["agent_id"] == foreign["agent_id"] and owned["turns"] == []
        denied = await command(
            ws,
            "resident.subscribe",
            "mismatched-resident",
            agent_id=foreign["agent_id"],
            thread_id=env.binding["thread_id"],
        )
        assert denied["type"] == "error" and denied["status"] == 404
        assert (await command(ws, "ping", "resident-still-connected"))["type"] == "result"
        assert len(env.invocations) == 0
    finally:
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values())


async def test_resident_subscription_error_is_scoped_and_task_keeps_streaming(
    resident_delivery, monkeypatch
):
    env = resident_delivery
    task_queue = asyncio.Queue()

    async def task_stream(services, owner, turn, history):
        while (chunk := await task_queue.get()) is not None:
            yield chunk

    patch_task_stream(monkeypatch, task_stream)
    session, ws = await connected(env.client, env.auth)
    try:
        await subscribe(ws, env.binding, "resident-before-revocation")
        created = await env.client.post(
            "/api/project-tasks",
            headers=env.auth,
            json={
                "request_id": uuid4().hex,
                "prompt": "Continue after another subscription fails.",
                "options": {"action_timeline_enabled": False},
            },
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["task_id"]
        await command(ws, "task.subscribe", "task-before-revocation", task_id=task_id)
        assert env.watches[env.binding["thread_id"]] == 1
        await asyncio.to_thread(
            env.server.services.agent_repo.set_enabled, env.binding["agent_id"], False
        )
        env.resident._updates.notify(env.binding["thread_id"])
        rejected = await until(ws, lambda frame: frame["type"] == "error")
        assert rejected["agent_id"] == env.binding["agent_id"]
        assert rejected["thread_id"] == env.binding["thread_id"]
        assert rejected["error"]["code"] == "AGENT_NOT_RUNNING"
        assert "id" not in rejected and "task_id" not in rejected
        assert env.watches[env.binding["thread_id"]] == 0
        task_queue.put_nowait({"type": "token", "content": "Task still publishes"})
        update = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn"
                and frame["data"]["task"]["task_id"] == task_id
                and frame["data"]["turn"]["reply"] == "Task still publishes"
            ),
        )
        assert update["data"]["turn"]["status"] == "running"
        assert (await command(ws, "ping", "after-resident-revocation"))["type"] == "result"
    finally:
        await asyncio.to_thread(
            env.server.services.agent_repo.set_enabled, env.binding["agent_id"], True
        )
        task_queue.put_nowait(None)
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values())


async def test_notification_during_resident_snapshot_read_wakes_another_read(
    resident_delivery, monkeypatch
):
    env = resident_delivery
    session, ws = await connected(env.client, env.auth)
    read_started, release_read = asyncio.Event(), asyncio.Event()
    original_snapshot = env.resident.snapshot
    intercept = False

    async def snapshot(*args, **kwargs):
        nonlocal intercept
        saved = await original_snapshot(*args, **kwargs)
        if intercept and saved.turns and saved.turns[-1].reply == "First":
            intercept = False
            read_started.set()
            await release_read.wait()
        return saved

    monkeypatch.setattr(env.resident, "snapshot", snapshot)
    try:
        await subscribe(ws, env.binding, "race-resident")
        accepted, queue = await send(env, "Do not lose a later commit.")
        await _turn(env.client, env.auth, env.base, accepted["request_id"], "running")
        intercept = True
        queue.put_nowait({"type": "token", "content": "First"})
        await asyncio.wait_for(read_started.wait(), 5)
        queue.put_nowait({"type": "token", "content": " second"})
        await receipt(env, accepted["request_id"], "First second")
        release_read.set()
        await next_snapshot(ws, env.binding, accepted["turn_id"], reply="First")
        await next_snapshot(ws, env.binding, accepted["turn_id"], reply="First second")
    finally:
        release_read.set()
        await session.__aexit__(None, None, None)


async def test_fallback_delivers_resident_commit_without_a_local_notice(
    resident_delivery, monkeypatch
):
    env = resident_delivery
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.02)
    accepted, queue = await send(env, "External database commit.")
    await _turn(env.client, env.auth, env.base, accepted["request_id"], "running")
    owner = await resolve_user_id(env.client, env.auth, "admin")
    repo = env.server.services.resident_request_repo
    row = await asyncio.to_thread(repo.get, owner, accepted["request_id"])
    original_snapshot = env.resident.snapshot
    reads = 0

    async def snapshot(*args, **kwargs):
        nonlocal reads
        result = await original_snapshot(*args, **kwargs)
        reads += 1
        return result

    monkeypatch.setattr(env.resident, "snapshot", snapshot)
    session, ws = await connected(env.client, env.auth)
    try:
        await subscribe(ws, env.binding, "external-resident")
        # Drain the extra read queued when installing the watch. Only the periodic
        # fallback may discover the subsequent write without a local notification.
        async with asyncio.timeout(5):
            while reads < 2:
                await asyncio.sleep(0.01)
        # SQL-only mutation omits the runtime's post-commit notification on purpose.
        assert await asyncio.to_thread(
            repo.update_reply,
            row.turn_id,
            row.worker_id,
            "Other process body",
            task_runtime.now_ms(),
        )
        await next_snapshot(ws, env.binding, accepted["turn_id"], reply="Other process body")
        queue.put_nowait({"type": "token", "content": "Other process body"})
        queue.put_nowait(None)
        await next_snapshot(
            ws, env.binding, accepted["turn_id"], reply="Other process body", status="completed"
        )
    finally:
        await session.__aexit__(None, None, None)
    assert not any(env.watches.values())
