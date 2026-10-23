"""Bridge Task frames preserve authority and do not leak the peer session JWT."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from octop.infra.bridge.task_streams import TaskStreamRelay
from octop.infra.bridge.transport import BridgeSession
from octop.infra.bridge.tunnel_policy import is_task_tunnel_path_allowed


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("POST", "/api/project-tasks", True),
        ("PATCH", "/api/projects/proj_x", True),
        ("GET", "/api/project-tasks/task_x/export", True),
        ("PUT", "/api/project-tasks/task_x/workspace/file", True),
        ("POST", "/api/project-tasks/task_x/recordings/rec_x/apply", True),
        ("GET", "/api/settings/timezone", True),
        ("POST", "/api/providers", False),
        ("GET", "/api/admin/users", False),
        ("GET", "/api/project-tasks/../admin/users", False),
        ("GET", "/api/project-tasks/%2e%2e", False),
        ("POST", "/api/project-tasks/task_x/export", False),
        ("GET", "/api/agents/private/memory", False),
        ("DELETE", "/api/knowledge-bases/kb/documents/doc/file", False),
    ],
)
def test_unified_task_proxy_has_explicit_method_and_path_policy(method, path, allowed):
    assert is_task_tunnel_path_allowed(method, path) is allowed


@pytest.mark.parametrize(
    "path",
    [
        "/api/project-tasks/ws",
        "/api/project-tasks/task_same/browser-stream/ws",
        "/api/project-tasks/task_same/terminal/ws",
    ],
)
async def test_task_socket_relay_routes_to_peer_asgi_and_strips_session_token(path):
    scopes = []
    outbound = []

    async def peer_app(scope, receive, send):
        scopes.append(scope)
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": '{"type":"ready","protocol_version":3}'})
        await send(
            {"type": "websocket.send", "text": '{"type":"session","access_token":"peer-secret"}'}
        )
        inbound = await receive()
        await send({"type": "websocket.send", "text": inbound["text"]})
        await send({"type": "websocket.close", "code": 1000})

    class Manager:
        def __init__(self):
            self.task_streams = TaskStreamRelay(self)
            self._asgi_app = peer_app

        def get_owned(self, cid, owner):
            assert cid == "connection" and owner == 7
            return SimpleNamespace(owner_user_id=7)

        def require_session(self, cid):
            assert cid == "connection"
            return self.session

        async def _owner_access_token(self, cid):
            return "peer-owner-auth"

    local, remote = Manager(), Manager()

    async def to_remote(raw):
        outbound.append(json.loads(raw))
        await remote.session.handle_message(raw)

    async def to_local(raw):
        await local.session.handle_message(raw)

    local.session = BridgeSession(
        connection_id="connection",
        send_text=to_remote,
        on_raw=lambda frame: local.task_streams.handle("connection", frame),
    )
    remote.session = BridgeSession(
        connection_id="connection",
        send_text=to_local,
        on_raw=lambda frame: remote.task_streams.handle("connection", frame),
    )

    class Socket:
        def __init__(self):
            self.frames = []
            self.inputs = asyncio.Queue()

        async def receive_json(self):
            return await self.inputs.get()

        async def send_json(self, frame):
            self.frames.append(frame)

        async def close(self, **kwargs):
            pass

    socket = Socket()
    await socket.inputs.put(
        {"type": "task.subscribe", "task_id": "task_same", "id": "subscribe-unique"}
    )
    allowed_query = {
        "expected_scope_revision": "scope-revision",
        "session_id": "terminal-session",
        "listen_only": "true",
        "width": "1024",
        "height": "768",
    }
    await asyncio.wait_for(
        local.task_streams.relay(
            "connection",
            7,
            path,
            {
                **allowed_query,
                "token": "local-auth-secret",
                "access_token": "local-access-secret",
                "arbitrary": "private-parameter",
                "profile_id": "private-profile",
                "path": "private-path",
            },
            socket,
        ),
        3,
    )
    assert [frame["type"] for frame in socket.frames] == ["ready", "task.subscribe"]
    assert "peer-secret" not in str(socket.frames)
    opened = [frame for frame in outbound if frame["type"] == "task-stream.open"]
    assert len(opened) == 1 and opened[0]["query"] == allowed_query
    assert not any(
        secret in str(outbound)
        for secret in (
            "local-auth-secret",
            "local-access-secret",
            "private-parameter",
            "private-profile",
            "private-path",
        )
    )
    assert scopes[0]["path"] == path
    assert (b"authorization", b"Bearer peer-owner-auth") in scopes[0]["headers"]
    assert parse_qs(scopes[0]["query_string"].decode()) == {
        **{key: [value] for key, value in allowed_query.items()},
        "token": ["peer-owner-auth"],
    }
    await local.task_streams.disconnect("connection")
    await remote.task_streams.disconnect("connection")
    assert not local.task_streams.waiters and not remote.task_streams.runners


@pytest.mark.parametrize(
    "kind,allowed",
    [
        ("task.create", True),
        ("task.subscribe", True),
        ("tool.result", True),
        ("ping", True),
        ("resident.subscribe", False),
        ("resident.unsubscribe", False),
        ("authenticate", False),
    ],
)
def test_main_stream_excludes_private_resident_and_generic_auth(kind, allowed):
    from octop.infra.bridge.task_streams import allows_task_stream_frame

    assert allows_task_stream_frame("/api/project-tasks/ws", {"type": kind}) is allowed


async def test_revoked_owner_token_during_open_closes_stream_and_cleans_client():
    from octop.infra.errors import ErrorCode, OctopError

    sent = []
    session = SimpleNamespace(send_json=None)

    async def send(frame):
        sent.append(frame)

    session.send_json = send

    async def revoked(cid):
        raise OctopError.localized(ErrorCode.AUTH_FAILED)

    manager = SimpleNamespace(require_session=lambda cid: session, _owner_access_token=revoked)
    relay = TaskStreamRelay(manager)
    rid = "a" * 32
    await relay.handle(
        "conn", {"type": "task-stream.open", "request_id": rid, "path": "/api/project-tasks/ws"}
    )
    await asyncio.gather(*relay.runners.values())
    await asyncio.sleep(0)
    assert not relay.clients and not relay.runners
    assert sent == [{"type": "task-stream.closed", "request_id": rid, "code": 1011}]


async def test_large_task_tunnel_response_returns_bounded_error(monkeypatch):
    from fastapi import FastAPI, Response

    from octop.infra.bridge import http_tunnel
    from octop.infra.bridge.http_tunnel import decode_body_b64, execute_local_http

    app = FastAPI()

    @app.get("/api/project-tasks/task_x/export")
    async def large():
        return Response(b"x" * 64)

    monkeypatch.setattr(http_tunnel, "MAX_TASK_TUNNEL_BODY_BYTES", 32)
    result = await execute_local_http(
        app=app, method="GET", path="/api/project-tasks/task_x/export", access_token="test-token"
    )
    assert result["status"] == 413
    assert b"TASK_ATTACHMENT_UNAVAILABLE" in decode_body_b64(result)
