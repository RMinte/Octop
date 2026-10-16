"""Task WebSocket commands, multiplexing and durable reconnects without LLM calls."""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from octop.infra.projects import runtime
from tests.support.auth import create_user, resolve_user_id
from tests.support.http import ws_connect, ws_token


async def connected(client, auth):
    session = ws_connect(client._octop_app, "/api/project-tasks/ws")
    ws = await session.__aenter__()
    await ws.send_json({"type": "authenticate", "token": ws_token(auth)})
    assert await ws.receive_json() == {"type": "ready", "protocol_version": 1}
    return session, ws


async def until(ws, predicate):
    for _ in range(100):
        frame = await ws.receive_json(timeout=5)
        if predicate(frame):
            return frame
    pytest.fail("expected task frame was not received")


async def command(ws, kind, key, **fields):
    await ws.send_json({"type": kind, "id": key, **fields})
    return await until(ws, lambda frame: frame.get("id") == key)


@pytest.fixture
async def ws_env(env_with_provider, monkeypatch):
    from octop.api.routers import project_tasks_ws

    client, server, auth = env_with_provider
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    calls = []
    gates = {}

    async def run(services, owner, turn, history):
        calls.append((turn, history))
        gate = gates.setdefault(turn.prompt, asyncio.Event())
        yield {"type": "token", "content": "实时中文"}
        await gate.wait()
        yield {"type": "token", "content": "，已经完成"}

    monkeypatch.setattr(runtime, "run_turn", run)
    yield client, server, auth, calls, gates
    for gate in gates.values():
        gate.set()


async def test_ws_streams_text_queues_other_tasks_and_cancels_exact_turn(ws_env):
    client, server, auth, calls, _ = ws_env
    experts = server.services.agent_repo.list_all()
    session, ws = await connected(client, auth)
    try:
        payload = {"request_id": "first-message", "prompt": "First"}
        result = await command(ws, "task.create", "create-first", data=payload)
        first = result["data"]
        same = await command(ws, "task.create", "create-again", data=payload)
        assert same["data"]["task_id"] == first["task_id"]
        await command(ws, "task.subscribe", "watch-first", task_id=first["task_id"])
        live = await until(
            ws,
            lambda f: f["type"] == "turn" and f["data"]["turn"]["reply"] == "实时中文",
        )
        assert live["data"]["turn"]["status"] == "running"
        second = (
            await command(
                ws,
                "task.create",
                "create-second",
                data={"request_id": "second-message", "prompt": "Second"},
            )
        )["data"]
        assert second["status"] == "queued" and len(calls) == 1
        await command(ws, "task.subscribe", "watch-second", task_id=second["task_id"])
        cancelled = await command(
            ws,
            "task.cancel",
            "cancel-first",
            task_id=first["task_id"],
            data={"turn_id": first["active_turn_id"]},
        )
        assert cancelled["data"]["turns"][0]["cancel_requested"]
        frames = []
        while not (
            any(f["data"]["turn"]["status"] == "cancelled" for f in frames)
            and any(
                f["data"]["task"]["task_id"] == second["task_id"]
                and f["data"]["turn"]["reply"] == "实时中文"
                for f in frames
            )
        ):
            frame = await ws.receive_json(timeout=5)
            if frame["type"] == "turn":
                frames.append(frame)
        assert len(calls) == 2
        assert server.services.agent_repo.list_all() == experts
        # Stop the queued/active second task too; cancellation is still independent of subscriptions.
        await command(
            ws,
            "task.cancel",
            "cancel-second",
            task_id=second["task_id"],
            data={"turn_id": second["active_turn_id"]},
        )
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_disconnect_keeps_execution_and_resumes_without_replaying(ws_env):
    client, _, auth, calls, gates = ws_env
    session, ws = await connected(client, auth)
    first = (
        await command(
            ws,
            "task.create",
            "create-first",
            data={"request_id": "durable-message", "prompt": "Durable"},
        )
    )["data"]
    await command(ws, "task.subscribe", "watch-first", task_id=first["task_id"])
    live = await until(
        ws, lambda f: f["type"] == "turn" and f["data"]["turn"]["reply"] == "实时中文"
    )
    await session.__aexit__(None, None, None)
    detail = (await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)).json()
    assert detail["status"] == "running" and not detail["turns"][0]["cancel_requested"]
    gates["Durable"].set()
    for _ in range(100):
        detail = (await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)).json()
        if detail["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    assert detail["status"] == "completed"
    session, ws = await connected(client, auth)
    try:
        await ws.send_json(
            {
                "type": "task.subscribe",
                "id": "resume-first",
                "task_id": first["task_id"],
                "cursor": live["data"]["cursor"],
            }
        )
        replay = await until(ws, lambda f: f["type"] == "turn")
        assert replay["data"]["turn"]["status"] == "completed"
        assert replay["data"]["turn"]["reply"] == "实时中文，已经完成"
        await until(ws, lambda f: f.get("id") == "resume-first")
        gates["Follow up"] = asyncio.Event()
        gates["Follow up"].set()
        next_round = await command(
            ws,
            "task.message",
            "message-next",
            task_id=first["task_id"],
            data={"request_id": "followup-message", "prompt": "Follow up"},
        )
        assert next_round["data"]["task_id"] == first["task_id"]
        await until(
            ws,
            lambda f: (
                f["type"] == "turn"
                and f["data"]["turn"]["turn_id"] == next_round["data"]["active_turn_id"]
                and f["data"]["turn"]["status"] == "completed"
            ),
        )
        assert len(calls) == 2
        assert calls[1][1][-1]["content"] == "实时中文，已经完成"
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_scopes_commands_and_cursors_without_dropping_other_tasks(ws_env):
    client, _, auth, _, _ = ws_env
    alice = await create_user(client, auth, username="ws-alice")
    session, ws = await connected(client, auth)
    first = (
        await command(
            ws,
            "task.create",
            "create-first",
            data={"request_id": "private-message", "prompt": "Private"},
        )
    )["data"]
    try:
        invalid = await command(
            ws,
            "task.subscribe",
            "bad-cursor-test",
            task_id=first["task_id"],
            cursor="bad-cursor",
        )
        assert invalid["status"] == 409 and invalid["error"]["code"] == "PROJECT_SYNC_RESET"
        assert (await command(ws, "ping", "connection-alive"))["type"] == "result"
        await ws.send_json({"type": "unknown", "secret": "never-echo-this"})
        invalid = await ws.receive_json()
        assert invalid["error"]["code"] == "TASK_FRAME_INVALID"
        assert "never-echo-this" not in str(invalid)
        foreign_session, foreign_ws = await connected(client, alice)
        try:
            for kind, fields in (
                ("task.subscribe", {}),
                ("task.message", {"data": {"request_id": "foreign-message", "prompt": "No"}}),
                ("task.cancel", {"data": {"turn_id": first["active_turn_id"]}}),
            ):
                denied = await command(
                    foreign_ws, kind, "foreign-command", task_id=first["task_id"], **fields
                )
                assert denied["status"] == 404
        finally:
            await foreign_session.__aexit__(None, None, None)
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_requires_authentication(env):
    client, _, _ = env
    async with ws_connect(client._octop_app, "/api/project-tasks/ws?token=not-accepted") as ws:
        await ws.send_json({"type": "authenticate", "token": "invalid-token"})
        assert (await ws.receive_json())["error"]["code"] == "AUTH_FAILED"
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json()
        assert closed.value.code == 4401


async def test_ws_worker_unavailable_does_not_create_empty_task(env_with_provider):
    client, server, auth = env_with_provider
    await server.project_task_worker.close()
    session, ws = await connected(client, auth)
    try:
        result = await command(
            ws,
            "task.create",
            "worker-unavailable",
            data={"request_id": "unavailable-message", "prompt": "Hello"},
        )
        assert result["error"]["code"] == "CONTROL_PLANE_BUSY"
        assert (await client.get("/api/project-tasks", headers=auth)).json()["items"] == []
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_native_header_auth_and_invalid_command_receipt(env):
    client, _, auth = env
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        invalid = await command(
            ws, "task.create", "invalid-command", data={"request_id": "valid-request", "prompt": ""}
        )
        assert invalid["id"] == "invalid-command" and invalid["status"] == 422
        assert (await command(ws, "ping", "still-connected"))["type"] == "result"


async def test_ws_rechecks_user_status_while_idle(env, monkeypatch):
    from octop.api.routers import project_tasks_ws

    client, _, admin = env
    alice = await create_user(client, admin, username="ws-reader")
    user_id = await resolve_user_id(client, admin, "ws-reader")
    monkeypatch.setattr(project_tasks_ws, "_AUTH_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    session, ws = await connected(client, alice)
    try:
        disabled = await client.patch(
            f"/api/users/{user_id}", headers=admin, json={"disabled": True}
        )
        assert disabled.status_code == 200
        rejected = await until(ws, lambda f: f["type"] == "error")
        assert rejected["error"]["code"] == "USER_DISABLED"
        with pytest.raises(WebSocketDisconnect) as closed:
            await ws.receive_json()
        assert closed.value.code == 4401
    finally:
        await session.__aexit__(None, None, None)


async def test_task_docs_render_rest_schemas_and_websocket_entry(env, monkeypatch):
    from dataclasses import replace

    import httpx

    from octop.api.app import build_app

    _, server, _ = env
    monkeypatch.setattr(
        server,
        "services",
        replace(server.services, config=replace(server.services.config, enable_api_docs=True)),
    )
    app = build_app(server)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        docs = await client.get("/api/docs")
        assert docs.status_code == 200 and "/api/openapi.json" in docs.text
        schema = (await client.get("/api/openapi.json")).json()
        tag = next(tag for tag in schema["tags"] if tag["name"] == "projects")
        assert "task.open" in tag["description"]
        assert "task.subscribe" in tag["description"]
        assert schema["components"]["schemas"]["TaskMessage"]["properties"]["prompt"]
