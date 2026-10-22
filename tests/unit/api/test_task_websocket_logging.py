"""Connection diagnostics survive transport failures and task cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import WebSocket
from starlette.datastructures import Headers
from starlette.websockets import WebSocketDisconnect

from octop.api.common.task_websocket_logging import TaskWebSocketLog
from octop.api.routers import project_tasks_ws
from octop.infra.errors import ErrorCode, OctopError

_LOG_NAME = "octop.api.common.task_websocket_logging"
_SECRET = "private-transport-error-sentinel"


@pytest.mark.parametrize(
    "request_id",
    [
        "B986F9AD-BBBC-4A7B-BB8D-9AB8F3319A07",
        "cb165a583db94fe7a8266446ac664303",
        "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    ],
)
def test_proxy_request_ids_support_uuid_hex_and_ulid(caplog, request_id):
    caplog.set_level(logging.INFO, logger=_LOG_NAME)
    diagnostics = TaskWebSocketLog(Headers({"X-Request-ID": request_id}))
    diagnostics.record("connection_received")
    record = next(record for record in caplog.records if record.name == _LOG_NAME)
    payload = json.loads(record.getMessage().removeprefix("project_task_ws "))
    assert payload["request_id"] == request_id


def test_duplicate_correlation_headers_are_rejected(caplog):
    caplog.set_level(logging.INFO, logger=_LOG_NAME)
    identifier = b"B986F9AD-BBBC-4A7B-BB8D-9AB8F3319A07"
    diagnostics = TaskWebSocketLog(
        Headers(
            raw=[
                (b"x-agentcomputer-connection-id", identifier),
                (b"x-agentcomputer-connection-id", identifier),
                (b"x-request-id", identifier),
                (b"x-request-id", identifier),
            ]
        )
    )
    diagnostics.record("connection_received")
    record = next(record for record in caplog.records if record.name == _LOG_NAME)
    payload = json.loads(record.getMessage().removeprefix("project_task_ws "))
    UUID(payload["server_connection_id"])
    assert payload["client_connection_id"] is None
    assert payload["client_connection_id_status"] == "invalid"
    assert payload["request_id"] is None
    assert b"x-agentcomputer-connection-id" not in dict(diagnostics.response_headers)
    assert identifier.decode() not in record.getMessage()


@pytest.mark.parametrize("server_close_sent", [False, True])
def test_later_peer_disconnect_preserves_known_transport_or_server_close(caplog, server_close_sent):
    caplog.set_level(logging.INFO, logger=_LOG_NAME)
    diagnostics = TaskWebSocketLog(Headers())
    if server_close_sent:
        diagnostics.record("close_sent", stage="close", close_code=1013)
    diagnostics.disconnected(OSError(_SECRET), stage="push")
    diagnostics.disconnected(WebSocketDisconnect(1006), stage="receive")
    diagnostics.record("connection_closed")
    records = [record for record in caplog.records if record.name == _LOG_NAME]
    closed = json.loads(records[-1].getMessage().removeprefix("project_task_ws "))
    assert closed["close_source"] == ("server" if server_close_sent else "transport")
    assert closed["close_stage"] == ("close" if server_close_sent else "push")
    if server_close_sent:
        assert closed["close_code"] == 1013
    for record in records:
        assert _SECRET not in record.getMessage()


@pytest.mark.parametrize(
    "failure",
    [
        "accept",
        "receive",
        "cancel",
        "error_send",
        "close_send",
        "pump_error_send",
        "pump_close_send",
    ],
)
async def test_connection_end_is_logged_once_on_transport_failure_or_cancellation(
    monkeypatch, caplog, failure
):
    caplog.set_level(logging.INFO, logger=_LOG_NAME)
    incoming = asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    ready = asyncio.Event()
    unexpected = failure in {"error_send", "close_send"}
    pump_failure = failure in {"pump_error_send", "pump_close_send"}
    if unexpected:
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "task.create",
                        "id": "failing-command",
                        "data": {"request_id": "failing-message", "prompt": _SECRET},
                    }
                ),
            }
        )

    async def receive():
        if failure == "receive" and ready.is_set():
            raise OSError(_SECRET)
        return await incoming.get()

    async def send(message):
        if failure == "accept" and message["type"] == "websocket.accept":
            raise OSError(_SECRET)
        if failure.endswith("close_send") and message["type"] == "websocket.close":
            if pump_failure:
                await incoming.put({"type": "websocket.disconnect", "code": 1006})
            raise OSError(_SECRET)
        if message["type"] == "websocket.send":
            kind = json.loads(message["text"])["type"]
            if failure.endswith("error_send") and kind == "error":
                if pump_failure:
                    await incoming.put({"type": "websocket.disconnect", "code": 1006})
                raise OSError(_SECRET)
            if kind == "ready":
                ready.set()

    websocket = WebSocket(
        {
            "type": "websocket",
            "headers": [(b"authorization", b"Bearer credential-secret-sentinel")],
            "app": SimpleNamespace(state=SimpleNamespace(octop_server=object())),
        },
        receive=receive,
        send=send,
    )
    monkeypatch.setattr(project_tasks_ws, "_services", Mock(return_value=object()))
    monkeypatch.setattr(
        project_tasks_ws,
        "resolve_user_from_token",
        Mock(return_value=SimpleNamespace(id=1, locale="en")),
    )
    monkeypatch.setattr(project_tasks_ws, "maybe_sliding_renew_token", Mock(return_value=None))
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0 if pump_failure else 3600)
    if pump_failure:
        monkeypatch.setattr(
            project_tasks_ws.approval_policy,
            "current",
            Mock(side_effect=OctopError(ErrorCode.CONTROL_PLANE_BUSY, _SECRET, status=503)),
        )
    if unexpected:
        monkeypatch.setattr(project_tasks_ws, "_task_worker", Mock(return_value=object()))
        monkeypatch.setattr(project_tasks_ws.tasks, "send", Mock(side_effect=RuntimeError(_SECRET)))
    handler = asyncio.create_task(project_tasks_ws.task_websocket(websocket))
    try:
        if failure == "cancel":
            await asyncio.wait_for(ready.wait(), timeout=5)
            handler.cancel()
            with pytest.raises(asyncio.CancelledError):
                await handler
        else:
            await asyncio.wait_for(handler, timeout=5)
    finally:
        handler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await handler

    records = [record for record in caplog.records if record.name == _LOG_NAME]
    diagnostics = [
        json.loads(record.getMessage().removeprefix("project_task_ws ")) for record in records
    ]
    received = [entry for entry in diagnostics if entry["event"] == "connection_received"]
    closed = [entry for entry in diagnostics if entry["event"] == "connection_closed"]
    assert len(received) == len(closed) == 1
    assert received[0]["server_connection_id"] == closed[0]["server_connection_id"]
    assert closed[0]["close_source"] == ("cancelled" if failure == "cancel" else "transport")
    assert closed[0]["command_count"] == (1 if unexpected else 0)
    assert closed[0]["subscription_count"] == 0
    assert diagnostics[-1]["event"] == "connection_closed"
    assert "close_sent" not in [entry["event"] for entry in diagnostics]
    if failure == "accept":
        assert "connection_accepted" not in [entry["event"] for entry in diagnostics]
    if failure != "cancel":
        transport = [entry for entry in diagnostics if entry["event"] == "transport_error"]
        assert len(transport) == 1 and transport[0]["exception_type"] == "OSError"
    if unexpected:
        failures = [entry for entry in diagnostics if entry["event"] == "unexpected_error"]
        assert len(failures) == 1 and failures[0]["exception_type"] == "RuntimeError"
    if pump_failure:
        failures = [entry for entry in diagnostics if entry["event"] == "error"]
        assert len(failures) == 1 and failures[0]["status"] == 503
        assert "unexpected_error" not in [entry["event"] for entry in diagnostics]
    for record in records:
        assert record.exc_info is None
        assert _SECRET not in record.getMessage()
        assert "credential-secret-sentinel" not in record.getMessage()
