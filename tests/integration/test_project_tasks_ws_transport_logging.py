"""Real Uvicorn/WebSocket handshake, close codes and private rotating logs."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from uuid import uuid4

import pytest
import uvicorn
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from octop.api.routers import project_tasks_ws
from octop.infra.errors import ErrorCode, OctopError
from tests.support.http import ws_token


@pytest.mark.parametrize("failure", [None, "service", "unexpected", "authentication"])
async def test_real_transport_correlates_ids_and_persists_without_content(
    env, monkeypatch, failure
):
    client, server, auth = env
    connection_id = str(uuid4()).upper()
    secret = "private-real-wire-sentinel"
    request_id = uuid4().hex
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    wire_client_logger = logging.getLogger("tests.websocket_client")
    monkeypatch.setattr(wire_client_logger, "level", logging.INFO)
    if failure == "service":

        def unavailable(*_args):
            raise OctopError(ErrorCode.CONTROL_PLANE_BUSY, secret, status=503)

        monkeypatch.setattr(project_tasks_ws, "_services", unavailable)
    elif failure == "unexpected":

        def unexpected(*_args):
            raise RuntimeError(secret)

        monkeypatch.setattr(project_tasks_ws.approval_policy, "current", unexpected)
        monkeypatch.setattr(project_tasks_ws, "_UPDATE_SECONDS", 0.01)
    elif failure == "authentication":

        def unauthenticated(*_args):
            raise OctopError(ErrorCode.AUTH_FAILED, secret, status=401)

        monkeypatch.setattr(project_tasks_ws, "resolve_user_from_token", unauthenticated)

    # Match real startup order: domain logging first, Uvicorn config second.
    # TRACE exercises the framework paths that would otherwise dump headers/frames.
    config = uvicorn.Config(
        client._octop_app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        log_level="trace",
        lifespan="off",
        ws="websockets",
    )
    runner = uvicorn.Server(config)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serving = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.setblocking(False)
        port = listener.getsockname()[1]
        serving = asyncio.create_task(runner.serve(sockets=[listener]))
        async with asyncio.timeout(10):
            while not runner.started:
                if serving.done():
                    await serving
                    pytest.fail("Uvicorn stopped before startup")
                await asyncio.sleep(0.01)
        async with connect(
            f"ws://127.0.0.1:{port}/api/project-tasks/ws?token={secret}",
            additional_headers={
                **auth,
                "X-AgentComputer-Connection-ID": connection_id,
                "X-Request-ID": request_id,
                "X-Private-Test-Header": secret,
            },
            # The test client's wire logger must not become a second source of
            # secret debug frames when Uvicorn enables protocol TRACE logging.
            logger=wire_client_logger,
        ) as ws:
            assert ws.response is not None
            headers = ws.response.headers
            assert headers["x-agentcomputer-connection-id"] == connection_id
            server_id = headers["x-octop-connection-id"]
            if failure in {None, "unexpected"}:
                assert json.loads(await ws.recv())["type"] == "ready"
            if failure is None:
                await ws.send(json.dumps({"type": "ping", "id": secret}))
                while json.loads(await ws.recv())["type"] != "result":
                    pass
                await ws.close(code=1000, reason=secret)
            else:
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await ws.recv())
                        if frame["type"] == "error":
                            break
                    assert (
                        frame["status"]
                        == {
                            "service": 503,
                            "unexpected": 500,
                            "authentication": 401,
                        }[failure]
                    )
                    with pytest.raises(ConnectionClosed) as closed:
                        await ws.recv()
                    assert closed.value.rcvd is not None
                    assert (
                        closed.value.rcvd.code
                        == {
                            "service": 1013,
                            "unexpected": 1011,
                            "authentication": 4401,
                        }[failure]
                    )
    finally:
        runner.should_exit = True
        if serving is not None:
            await asyncio.wait_for(serving, timeout=10)
        listener.close()

    # Read the real rotating file, rather than pytest's in-memory log capture.
    for handler in logging.getLogger().handlers:
        handler.flush()
    persisted = server.paths.log.read_text(encoding="utf-8")
    assert secret not in persisted
    assert ws_token(auth) not in persisted
    records = [
        json.loads(line.split("project_task_ws ", 1)[1])
        for line in persisted.splitlines()
        if "project_task_ws " in line
    ]
    assert records
    assert {record["server_connection_id"] for record in records} == {server_id}
    assert {record["client_connection_id"] for record in records} == {connection_id}
    assert {record["request_id"] for record in records} == {request_id}
    events = [record["event"] for record in records]
    assert events.count("connection_received") == events.count("connection_closed") == 1
    assert events[0] == "connection_received" and events[-1] == "connection_closed"
    assert [record["seq"] for record in records] == list(range(1, len(records) + 1))
