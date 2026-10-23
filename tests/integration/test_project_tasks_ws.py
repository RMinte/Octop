"""Task WebSocket commands, multiplexing and durable reconnects without LLM calls."""

from __future__ import annotations

import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import runtime
from tests.support.auth import create_user, resolve_user_id
from tests.support.http import ws_connect, ws_token


async def connected(client, auth):
    session = ws_connect(client._octop_app, "/api/project-tasks/ws")
    ws = await session.__aenter__()
    await ws.send_json({"type": "authenticate", "token": ws_token(auth)})
    assert await ws.receive_json() == {"type": "ready", "protocol_version": 3}
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


async def assert_connection_error(ws, *, status, code, close_code):
    rejected = await until(ws, lambda frame: frame["type"] == "error")
    assert rejected["status"] == status and rejected["error"]["code"] == code
    with pytest.raises(WebSocketDisconnect) as closed:
        await ws.receive_json()
    assert closed.value.code == close_code


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


async def test_ws_streams_tasks_concurrently_and_cancels_exact_turn(ws_env, monkeypatch):
    client, server, auth, calls, gates = ws_env
    subscribed = asyncio.Event()
    second_streamed = asyncio.Event()
    original_run = runtime.run_turn

    async def run(services, owner, turn, history):
        await subscribed.wait()
        async for event in original_run(services, owner, turn, history):
            yield event
            if turn.prompt == "Second" and event.get("content") == "实时中文":
                # The worker has consumed and persisted this token before the
                # generator resumes. Subscribe after it to cover snapshot catch-up.
                second_streamed.set()

    monkeypatch.setattr(runtime, "run_turn", run)
    experts = server.services.agent_repo.list_all()
    session, ws = await connected(client, auth)
    try:
        payload = {"request_id": "first-message", "prompt": "First"}
        result = await command(ws, "task.create", "create-first", data=payload)
        first = result["data"]
        same = await command(ws, "task.create", "create-again", data=payload)
        assert same["data"]["task_id"] == first["task_id"]
        await command(ws, "task.subscribe", "watch-first", task_id=first["task_id"])
        # Publish after subscription is acknowledged to exercise a new live frame.
        subscribed.set()
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
        assert second["status"] == "queued"
        await asyncio.wait_for(second_streamed.wait(), 5)
        assert len(calls) == 2
        await ws.send_json(
            {"type": "task.subscribe", "id": "watch-second", "task_id": second["task_id"]}
        )
        snapshot = await until(
            ws,
            lambda frame: (
                frame["type"] == "snapshot" and frame["data"]["task_id"] == second["task_id"]
            ),
        )
        assert snapshot["data"]["status"] == "running"
        assert snapshot["data"]["turns"][0]["reply"] == "实时中文"
        assert snapshot["data"]["turns"][0]["turn_id"] == second["active_turn_id"]
        await until(ws, lambda frame: frame.get("id") == "watch-second")
        await ws.send_json(
            {
                "type": "task.cancel",
                "id": "cancel-first",
                "task_id": first["task_id"],
                "data": {"turn_id": first["active_turn_id"]},
            }
        )
        cancelled, terminal = None, None
        while cancelled is None or terminal is None:
            frame = await ws.receive_json(timeout=5)
            if frame.get("id") == "cancel-first":
                cancelled = frame
            elif (
                frame["type"] == "turn"
                and frame["data"]["turn"]["turn_id"] == first["active_turn_id"]
                and frame["data"]["turn"]["status"] == "cancelled"
            ):
                terminal = frame
        assert cancelled["type"] == "result"
        assert cancelled["data"]["turns"][0]["cancel_requested"]
        assert terminal["data"]["turn"]["reply"] == "实时中文"
        independent = (
            await client.get(f"/api/project-tasks/{second['task_id']}", headers=auth)
        ).json()
        assert independent["status"] == "running"
        assert independent["active_turn_id"] == second["active_turn_id"]
        assert not independent["turns"][0]["cancel_requested"]
        gates["Second"].set()
        completed = await until(
            ws,
            lambda frame: (
                frame["type"] == "turn"
                and frame["data"]["turn"]["turn_id"] == second["active_turn_id"]
                and frame["data"]["turn"]["status"] == "completed"
            ),
        )
        assert completed["data"]["turn"]["reply"] == "实时中文，已经完成"
        assert len(calls) == 2
        assert server.services.agent_repo.list_all() == experts
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_disconnect_keeps_execution_and_resumes_without_replaying(ws_env, monkeypatch):
    client, _, auth, calls, gates = ws_env
    subscribed = asyncio.Event()
    original_run = runtime.run_turn

    async def run(services, owner, turn, history):
        await subscribed.wait()
        async for event in original_run(services, owner, turn, history):
            yield event

    monkeypatch.setattr(runtime, "run_turn", run)
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
    subscribed.set()
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
        await ws.send_json(
            {
                "type": "task.message",
                "id": "message-next",
                "task_id": first["task_id"],
                "data": {"request_id": "followup-message", "prompt": "Follow up"},
            }
        )
        next_round = None
        streamed = []
        while next_round is None:
            frame = await ws.receive_json(timeout=5)
            if frame.get("id") == "message-next":
                next_round = frame
            elif frame["type"] == "turn":
                streamed.append(frame)
        assert next_round["data"]["task_id"] == first["task_id"]

        def completed(frame):
            return (
                frame["type"] == "turn"
                and frame["data"]["turn"]["turn_id"] == next_round["data"]["active_turn_id"]
                and frame["data"]["turn"]["status"] == "completed"
            )

        final = next((frame for frame in streamed if completed(frame)), None)
        if final is None:
            final = await until(ws, completed)
        assert final["data"]["turn"]["reply"] == "实时中文，已经完成"
        assert len(calls) == 2
        assert calls[1][1][-1]["content"] == "实时中文，已经完成"
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_service_recovery_restores_task_with_same_token_without_replaying(
    ws_env, monkeypatch
):
    from octop.api.routers import project_tasks_ws

    client, _, auth, calls, _ = ws_env
    session, ws = await connected(client, auth)
    try:
        first = (
            await command(
                ws,
                "task.create",
                "create-recoverable",
                data={"request_id": "recoverable-message", "prompt": "Recoverable"},
            )
        )["data"]
        for _ in range(100):
            detail = (
                await client.get(f"/api/project-tasks/{first['task_id']}", headers=auth)
            ).json()
            if detail["turns"][0]["reply"] == "实时中文":
                break
            await asyncio.sleep(0.01)
        assert detail["turns"][0]["reply"] == "实时中文"
        assert len(calls) == 1

        def unavailable(*_args, **_kwargs):
            raise OctopError(ErrorCode.INTERNAL_ERROR, "temporary policy failure", status=503)

        with monkeypatch.context() as patch:
            patch.setattr(project_tasks_ws.approval_policy, "current", unavailable)
            await assert_connection_error(ws, status=503, code="INTERNAL_ERROR", close_code=1013)
    finally:
        await session.__aexit__(None, None, None)

    session, ws = await connected(client, auth)
    try:
        await ws.send_json(
            {"type": "task.subscribe", "id": "recover-task", "task_id": first["task_id"]}
        )
        recovered = (await until(ws, lambda frame: frame["type"] == "snapshot"))["data"]
        assert recovered["task_id"] == first["task_id"]
        assert recovered["turns"][0]["turn_id"] == first["active_turn_id"]
        assert recovered["turns"][0]["reply"] == "实时中文"
        assert recovered["status"] == "running" and not recovered["turns"][0]["cancel_requested"]
        assert (await until(ws, lambda frame: frame.get("id") == "recover-task"))[
            "type"
        ] == "result"
        assert len(calls) == 1
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
        invalid = await until(ws, lambda frame: frame["type"] == "error")
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


async def test_ws_unbound_database_accepts_before_reporting_service_unavailable(env, monkeypatch):
    client, server, _ = env
    with monkeypatch.context() as patch:
        patch.setattr(server, "services", None)
        # Entering the session proves the first ASGI event is accept, not a
        # pre-handshake close that a real server would turn into HTTP 403.
        async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
            await assert_connection_error(ws, status=503, code="SETUP_REQUIRED", close_code=1013)


@pytest.mark.parametrize(
    ("phase", "code", "status", "close_code"),
    [
        ("initial", ErrorCode.INTERNAL_ERROR, 503, 1013),
        ("command", ErrorCode.INTERNAL_ERROR, 503, 1013),
        ("idle", ErrorCode.INTERNAL_ERROR, 503, 1013),
        ("push", ErrorCode.INTERNAL_ERROR, 503, 1013),
        ("initial", ErrorCode.AUTH_FAILED, 503, 1013),
        ("initial", ErrorCode.FORBIDDEN, 403, 1011),
    ],
)
async def test_ws_connection_failures_do_not_claim_login_expired(
    env, monkeypatch, phase, code, status, close_code
):
    from octop.api.routers import project_tasks_ws

    client, _, auth = env
    monkeypatch.setattr(project_tasks_ws, "_AUTH_CHECK_SECONDS", 0 if phase == "idle" else 3600)
    monkeypatch.setattr(
        project_tasks_ws, "_UPDATE_SECONDS", 0.01 if phase in {"idle", "push"} else 3600
    )

    def unavailable(*_args, **_kwargs):
        raise OctopError(code, "injected connection failure", status=status)

    if phase == "initial":
        monkeypatch.setattr(project_tasks_ws, "resolve_user_from_token", unavailable)
        async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
            await assert_connection_error(ws, status=status, code=code.value, close_code=close_code)
        return

    session, ws = await connected(client, auth)
    try:
        if phase == "push":
            monkeypatch.setattr(project_tasks_ws.approval_policy, "current", unavailable)
        else:
            monkeypatch.setattr(project_tasks_ws, "resolve_user_from_token", unavailable)
        if phase == "command":
            await ws.send_json({"type": "ping", "id": "recheck-service"})
        await assert_connection_error(ws, status=status, code=code.value, close_code=close_code)
    finally:
        await session.__aexit__(None, None, None)


async def test_ws_missing_jwt_secret_reports_internal_error(env, monkeypatch):
    client, server, auth = env
    original_get = server.services.secret_repo.get
    monkeypatch.setattr(
        server.services.secret_repo,
        "get",
        lambda key: None if key == "jwt" else original_get(key),
    )
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await ws.send_json({"type": "authenticate", "token": ws_token(auth)})
        await assert_connection_error(ws, status=500, code="INTERNAL_ERROR", close_code=1011)


async def test_ws_expired_token_still_reports_authentication_failure(env):
    from octop.api.deps import sign_token

    client, server, _ = env
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    expired = sign_token(secret, sub=1, uname="expired", role="user", ttl_seconds=-1)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await ws.send_json({"type": "authenticate", "token": expired})
        await assert_connection_error(ws, status=401, code="TOKEN_EXPIRED", close_code=4401)


async def test_ws_command_service_failure_keeps_connection(env_with_provider, monkeypatch):
    from octop.api.routers import project_tasks_ws

    client, _, auth = env_with_provider

    def unavailable(*_args, **_kwargs):
        raise OctopError(ErrorCode.INTERNAL_ERROR, "injected command failure", status=503)

    monkeypatch.setattr(project_tasks_ws.tasks, "send", unavailable)
    session, ws = await connected(client, auth)
    try:
        result = await command(
            ws,
            "task.create",
            "unavailable-command",
            data={"request_id": "service-failure", "prompt": "Hello"},
        )
        assert result["status"] == 503 and result["error"]["code"] == "INTERNAL_ERROR"
        assert (await command(ws, "ping", "still-connected"))["type"] == "result"
    finally:
        await session.__aexit__(None, None, None)


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
