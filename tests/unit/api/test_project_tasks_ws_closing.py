"""Connection shutdown must finish before a queued command can run."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import WebSocket

from octop.api.routers import project_tasks_ws
from octop.infra.errors import ErrorCode, OctopError


async def test_push_failure_closes_before_queued_command(monkeypatch, caplog):
    incoming = asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    failure_started = asyncio.Event()
    release_failure = asyncio.Event()
    command_received = asyncio.Event()
    outgoing = []

    async def receive():
        event = await incoming.get()
        if event["type"] == "websocket.receive":
            command_received.set()
        return event

    async def send(event):
        outgoing.append(event)
        if event["type"] == "websocket.send":
            frame = json.loads(event["text"])
            if frame["type"] == "error":
                failure_started.set()
                await release_failure.wait()

    websocket = WebSocket(
        {
            "type": "websocket",
            "headers": [(b"authorization", b"Bearer test-token")],
            "app": SimpleNamespace(state=SimpleNamespace(octop_server=object())),
        },
        receive=receive,
        send=send,
    )
    user = SimpleNamespace(id=1, locale="en")
    resolve_user = Mock(return_value=user)
    monkeypatch.setattr(project_tasks_ws, "_services", Mock(return_value=object()))
    monkeypatch.setattr(project_tasks_ws, "resolve_user_from_token", resolve_user)
    monkeypatch.setattr(project_tasks_ws, "maybe_sliding_renew_token", Mock(return_value=None))
    monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0)
    monkeypatch.setattr(
        project_tasks_ws.approval_policy,
        "current",
        Mock(side_effect=OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY, status=503)),
    )
    caplog.set_level(logging.ERROR, logger=project_tasks_ws.__name__)
    handler = asyncio.create_task(project_tasks_ws.task_websocket(websocket))
    try:
        await asyncio.wait_for(failure_started.wait(), timeout=5)
        await incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps({"type": "ping", "id": "queued-command"}),
            }
        )
        await asyncio.wait_for(command_received.wait(), timeout=5)
        release_failure.set()
        await asyncio.wait_for(handler, timeout=5)
    finally:
        release_failure.set()
        handler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await handler

    assert [event["type"] for event in outgoing] == [
        "websocket.accept",
        "websocket.send",
        "websocket.send",
        "websocket.close",
    ]
    assert json.loads(outgoing[1]["text"]) == {"type": "ready", "protocol_version": 3}
    failure = json.loads(outgoing[2]["text"])
    assert failure["type"] == "error" and failure["status"] == 503
    assert failure["error"]["code"] == "CONTROL_PLANE_BUSY"
    assert outgoing[3] == {
        "type": "websocket.close",
        "code": 1013,
        "reason": "CONTROL_PLANE_BUSY",
    }
    assert resolve_user.call_count == 1
    assert not caplog.records
