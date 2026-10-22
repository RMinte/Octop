"""Connection diagnostics correlate native clients without retaining content."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from starlette.websockets import WebSocketDisconnect

from octop.api.routers import project_tasks_ws
from octop.infra.errors import ErrorCode, OctopError
from tests.support.http import ws_connect, ws_token

_LOG_NAME = "octop.api.common.task_websocket_logging"
_PREFIX = "project_task_ws "
_CLIENT_ID = "B986F9AD-BBBC-4A7B-BB8D-9AB8F3319A07"
_SECRET = "private-diagnostic-sentinel"


@pytest.fixture
def diagnostics(caplog):
    caplog.set_level(logging.INFO, logger=_LOG_NAME)

    def records():
        result = []
        for record in caplog.records:
            if record.name == _LOG_NAME:
                message = record.getMessage()
                assert message.startswith(_PREFIX)
                assert record.exc_info is None
                result.append(json.loads(message.removeprefix(_PREFIX)))
        return result

    return records


def event(records, kind):
    matches = [record for record in records if record["event"] == kind]
    assert len(matches) == 1, (kind, records)
    return matches[0]


def assert_private(caplog, *secrets):
    records = [
        record for record in caplog.records if record.name in {_LOG_NAME, project_tasks_ws.__name__}
    ]
    for record in records:
        assert record.exc_info is None
        message = record.getMessage()
        for secret in secrets:
            assert secret not in message


def capture_accept(app, events):
    async def wrapped(scope, receive, send):
        async def captured_send(message):
            if message["type"] == "websocket.accept":
                events.append(message)
            await send(message)

        await app(scope, receive, captured_send)

    return wrapped


async def next_frame(ws, kind):
    for _ in range(30):
        frame = await ws.receive_json(timeout=5)
        if frame["type"] == kind:
            return frame
    pytest.fail(f"expected {kind} frame")


async def assert_failure(ws, status, code, close_code):
    frame = await next_frame(ws, "error")
    assert frame["status"] == status
    assert frame["error"]["code"] == code
    with pytest.raises(WebSocketDisconnect) as closed:
        await ws.receive_json()
    assert closed.value.code == close_code


async def test_diagnostics_correlate_header_auth_and_peer_disconnect(env, diagnostics, caplog):
    client, _, auth = env
    proxy_request_id = "cb165a583db94fe7a8266446ac664303"
    accepted = []
    app = capture_accept(client._octop_app, accepted)
    async with ws_connect(
        app,
        "/api/project-tasks/ws?token=query-private-sentinel",
        headers={
            **auth,
            "X-AgentComputer-Connection-ID": _CLIENT_ID,
            "X-Request-ID": proxy_request_id,
        },
    ) as ws:
        assert await ws.receive_json() == {"type": "ready", "protocol_version": 3}
        await ws.send_json({"type": "ping", "id": "diagnostic-ping"})
        assert (await next_frame(ws, "result"))["id"] == "diagnostic-ping"

    records = diagnostics()
    received = event(records, "connection_received")
    server_id = received["server_connection_id"]
    UUID(server_id)
    assert {record["server_connection_id"] for record in records} == {server_id}
    assert {record["client_connection_id"] for record in records} == {_CLIENT_ID}
    assert {record["client_connection_id_status"] for record in records} == {"valid"}
    assert {record["request_id"] for record in records} == {proxy_request_id}
    assert {record["path"] for record in records} == {"/api/project-tasks/ws"}
    assert {record["schema_version"] for record in records} == {1}
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
    elapsed = [record["elapsed_ms"] for record in records]
    assert elapsed == sorted(elapsed) and elapsed[0] >= 0
    for record in records:
        assert (
            datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            .utcoffset()
            .total_seconds()
            == 0
        )
    kinds = [record["event"] for record in records]
    assert kinds.index("connection_received") < kinds.index("connection_accepted")
    assert kinds.index("authentication_started") < kinds.index("authentication_succeeded")
    assert kinds.index("authentication_succeeded") < kinds.index("ready")
    closed = event(records, "connection_closed")
    assert closed["close_code"] == 1000 and closed["close_source"] == "peer"
    assert closed["command_count"] == 1 and closed["subscription_count"] == 0
    headers = dict(accepted[0]["headers"])
    assert headers[b"x-octop-connection-id"].decode() == server_id
    assert headers[b"x-agentcomputer-connection-id"].decode() == _CLIENT_ID
    assert_private(caplog, ws_token(auth), "query-private-sentinel", "diagnostic-ping")


@pytest.mark.parametrize(
    "client_id", [None, _SECRET, _CLIENT_ID + "," + _CLIENT_ID, _SECRET + "\r\nforged"]
)
async def test_diagnostics_drop_untrusted_connection_and_request_ids(
    env, diagnostics, caplog, client_id
):
    client, _, auth = env
    accepted = []
    headers = {**auth, "X-Request-ID": _SECRET}
    if client_id is not None:
        headers["X-AgentComputer-Connection-ID"] = client_id
    async with ws_connect(
        capture_accept(client._octop_app, accepted), "/api/project-tasks/ws", headers=headers
    ) as ws:
        assert (await ws.receive_json())["type"] == "ready"
    records = diagnostics()
    server_id = event(records, "connection_received")["server_connection_id"]
    UUID(server_id)
    assert {record["client_connection_id"] for record in records} == {None}
    assert {record["client_connection_id_status"] for record in records} == {
        "missing" if client_id is None else "invalid"
    }
    assert {record["request_id"] for record in records} == {None}
    response_headers = dict(accepted[0]["headers"])
    assert b"x-agentcomputer-connection-id" not in response_headers
    assert response_headers[b"x-octop-connection-id"].decode() == server_id
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_reconnect_uses_new_server_id_with_same_client_id(env, diagnostics):
    client, _, auth = env
    for _ in range(2):
        async with ws_connect(
            client._octop_app,
            "/api/project-tasks/ws",
            headers={**auth, "X-AgentComputer-Connection-ID": _CLIENT_ID},
        ) as ws:
            assert (await ws.receive_json())["type"] == "ready"
    records = diagnostics()
    received = [record for record in records if record["event"] == "connection_received"]
    closed = [record for record in records if record["event"] == "connection_closed"]
    assert len(received) == len(closed) == 2
    server_ids = {record["server_connection_id"] for record in received}
    assert len(server_ids) == 2
    assert {record["server_connection_id"] for record in closed} == server_ids
    assert {record["client_connection_id"] for record in records} == {_CLIENT_ID}


async def test_service_not_ready_records_503_and_1013_after_accept(env, monkeypatch, diagnostics):
    client, server, _ = env
    monkeypatch.setattr(server, "services", None)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await assert_failure(ws, 503, "SETUP_REQUIRED", 1013)
    records = diagnostics()
    assert event(records, "error")["status"] == 503
    assert event(records, "error")["error_code"] == "SETUP_REQUIRED"
    assert event(records, "close_sent")["close_code"] == 1013
    assert event(records, "connection_closed")["close_source"] == "server"
    assert event(records, "connection_closed")["close_code"] == 1013
    assert "authentication_started" not in [record["event"] for record in records]
    assert records[0]["event"] == "connection_received"
    assert records[-1]["event"] == "connection_closed"


@pytest.mark.parametrize(
    ("code", "status", "close_code"),
    [(ErrorCode.AUTH_FAILED, 401, 4401), (ErrorCode.INTERNAL_ERROR, 503, 1013)],
)
async def test_authentication_failure_classifies_auth_and_service_errors(
    env, monkeypatch, diagnostics, caplog, code, status, close_code
):
    client, _, auth = env

    def fail(*_args, **_kwargs):
        raise OctopError(code, _SECRET, status=status, details={"token": _SECRET})

    monkeypatch.setattr(project_tasks_ws, "resolve_user_from_token", fail)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        await assert_failure(ws, status, code.value, close_code)
    records = diagnostics()
    assert event(records, "authentication_failed")["status"] == status
    assert event(records, "error")["error_code"] == code.value
    assert event(records, "close_sent")["close_code"] == close_code
    assert event(records, "connection_closed")["close_code"] == close_code
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_invalid_authentication_frame_never_logs_token_or_frame(env, diagnostics, caplog):
    client, _, _ = env
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await ws.send_json({"type": "authenticate", "token": _SECRET, "prompt": _SECRET})
        await assert_failure(ws, 401, "AUTH_FAILED", 4401)
    assert event(diagnostics(), "authentication_failed")["error_code"] == "AUTH_FAILED"
    assert_private(caplog, _SECRET)


async def test_push_service_failure_records_503_without_claiming_login_expired(
    env, monkeypatch, diagnostics, caplog
):
    client, _, auth = env
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)

    def fail(*_args, **_kwargs):
        raise OctopError(ErrorCode.CONTROL_PLANE_BUSY, _SECRET, status=503)

    monkeypatch.setattr(project_tasks_ws.approval_policy, "current", fail)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        await assert_failure(ws, 503, "CONTROL_PLANE_BUSY", 1013)
    records = diagnostics()
    assert event(records, "error")["scope"] == "connection"
    assert event(records, "close_sent")["close_code"] == 1013
    assert event(records, "connection_closed")["error_count"] == 1
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_command_error_keeps_connection_and_omits_command_content(
    env_with_provider, monkeypatch, diagnostics, caplog
):
    client, _, auth = env_with_provider

    def fail(*_args, **_kwargs):
        raise OctopError(ErrorCode.INTERNAL_ERROR, _SECRET, status=503)

    monkeypatch.setattr(project_tasks_ws.tasks, "send", fail)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_json(
            {
                "type": "task.create",
                "id": _SECRET,
                "data": {"request_id": _SECRET, "prompt": _SECRET},
            }
        )
        frame = await next_frame(ws, "error")
        assert frame["status"] == 503 and frame["id"] == _SECRET
        await ws.send_json({"type": "ping", "id": "continue-command"})
        assert (await next_frame(ws, "result"))["id"] == "continue-command"
    records = diagnostics()
    failure = event(records, "error")
    assert failure["scope"] == "command" and failure["command_type"] == "task.create"
    assert "close_sent" not in [record["event"] for record in records]
    assert event(records, "connection_closed")["command_count"] == 2
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_unexpected_failure_records_type_without_exception_text(
    env_with_provider, monkeypatch, diagnostics, caplog
):
    client, _, auth = env_with_provider

    def fail(*_args, **_kwargs):
        raise RuntimeError(_SECRET)

    monkeypatch.setattr(project_tasks_ws.tasks, "send", fail)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_json(
            {
                "type": "task.create",
                "id": "diagnostic-command",
                "data": {"request_id": "diagnostic-message", "prompt": _SECRET},
            }
        )
        await assert_failure(ws, 500, "INTERNAL_ERROR", 1011)
    records = diagnostics()
    assert event(records, "unexpected_error")["exception_type"] == "RuntimeError"
    assert event(records, "close_sent")["close_code"] == 1011
    assert event(records, "connection_closed")["close_code"] == 1011
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_subscription_error_omits_task_identity_and_keeps_connection(
    env, monkeypatch, diagnostics, caplog
):
    client, _, auth = env
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    monkeypatch.setattr(
        project_tasks_ws.tasks,
        "detail",
        lambda *_args: SimpleNamespace(event_cursor="test-cursor", model_dump=lambda **_kw: {}),
    )
    calls = 0

    def updates(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return []
        raise OctopError(ErrorCode.NOT_FOUND, _SECRET)

    monkeypatch.setattr(project_tasks_ws.tasks, "updates", updates)
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_json({"type": "task.subscribe", "id": _SECRET, "task_id": _SECRET})
        assert (await next_frame(ws, "result"))["id"] == _SECRET
        failure = await next_frame(ws, "error")
        assert failure["status"] == 404 and failure["task_id"] == _SECRET
        await ws.send_json({"type": "ping", "id": "after-subscription-error"})
        assert (await next_frame(ws, "result"))["id"] == "after-subscription-error"
    records = diagnostics()
    assert event(records, "error")["scope"] == "subscription"
    assert "close_sent" not in [record["event"] for record in records]
    assert event(records, "connection_closed")["subscription_count"] == 0
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_invalid_command_type_and_payload_are_not_retained(env, diagnostics, caplog):
    client, _, auth = env
    async with ws_connect(client._octop_app, "/api/project-tasks/ws", headers=auth) as ws:
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_json({"type": _SECRET, "id": _SECRET, "prompt": _SECRET})
        assert (await next_frame(ws, "error"))["error"]["code"] == "TASK_FRAME_INVALID"
        await ws.send_json({"type": "ping", "id": "after-invalid"})
        assert (await next_frame(ws, "result"))["id"] == "after-invalid"
    failure = event(diagnostics(), "error")
    assert failure["scope"] == "command" and failure.get("command_type") is None
    assert_private(caplog, _SECRET, ws_token(auth))


async def test_session_renewal_is_recorded_without_old_or_new_token(env, diagnostics, caplog):
    from octop.api.deps import resolve_user_from_token, sign_token

    client, server, auth = env
    user = resolve_user_from_token(server, ws_token(auth))
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    short_token = sign_token(
        secret, sub=user.id, uname=user.username, role=user.role, ttl_seconds=30
    )
    async with ws_connect(client._octop_app, "/api/project-tasks/ws") as ws:
        await ws.send_json({"type": "authenticate", "token": short_token})
        assert (await ws.receive_json())["type"] == "ready"
        await ws.send_json({"type": "ping", "id": "renew-command"})
        renewal = await next_frame(ws, "session")
        renewed_token = renewal["access_token"]
        assert renewed_token != short_token
        assert (await next_frame(ws, "result"))["id"] == "renew-command"
    records = diagnostics()
    event(records, "session_renewed")
    assert event(records, "connection_closed")["auth_check_count"] >= 1
    assert_private(caplog, repr(secret), secret.hex(), short_token, renewed_token, ws_token(auth))
