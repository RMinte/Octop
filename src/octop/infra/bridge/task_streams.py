"""Scope-limited Task WebSockets over an authenticated Bridge connection."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

_MAIN_COMMANDS = frozenset(
    {
        "task.create",
        "task.message",
        "task.retry",
        "task.cancel",
        "task.pause",
        "task.resume",
        "task.approval.set",
        "task.interaction.respond",
        "task.subscribe",
        "task.open",
        "task.unsubscribe",
        "ping",
        "client.attach",
        "tool.get",
        "tool.claim",
        "tool.result",
        "tool.review",
    }
)
_STREAM_QUERY_KEYS = frozenset(
    {"expected_scope_revision", "session_id", "listen_only", "width", "height"}
)


def allows_task_stream_frame(path: str, frame: dict[str, Any]) -> bool:
    # The peer socket also serves private resident subscriptions. That surface
    # is deliberately absent from the remote unified-Task contract.
    return path != "/api/project-tasks/ws" or frame.get("type") in _MAIN_COMMANDS


class TaskStreamRelay:
    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.waiters: dict[tuple[str, str], asyncio.Queue[dict[str, Any]]] = {}
        self.clients: dict[tuple[str, str], asyncio.Queue[dict[str, Any]]] = {}
        self.runners: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def handle(self, connection_id: str, frame: dict[str, Any]) -> None:
        kind = str(frame.get("type") or "")
        if not kind.startswith("task-stream."):
            return
        request_id = str(frame.get("request_id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", request_id):
            return
        key = (connection_id, request_id)
        if kind in {"task-stream.server", "task-stream.closed"}:
            if queue := self.waiters.get(key):
                if queue.full():
                    # A slow subscriber must not stall the shared Bridge reader.
                    # Close only this stream; reconnect obtains its saved state.
                    queue.get_nowait()
                    queue.put_nowait({"type": "task-stream.closed", "code": 1013})
                else:
                    queue.put_nowait(frame)
        elif kind == "task-stream.open":
            if key in self.runners:
                return
            queue = asyncio.Queue[dict[str, Any]](maxsize=128)
            self.clients[key] = queue
            runner = asyncio.create_task(self._serve(key, frame, queue))
            self.runners[key] = runner
            runner.add_done_callback(lambda _: self.runners.pop(key, None))
        elif kind == "task-stream.client":
            if queue := self.clients.get(key):
                message = frame.get("frame")
                if isinstance(message, dict):
                    if queue.full():
                        if stalled_runner := self.runners.get(key):
                            stalled_runner.cancel()
                    else:
                        queue.put_nowait(message)
        elif kind == "task-stream.close" and (task := self.runners.get(key)):
            task.cancel()

    async def _serve(
        self, key: tuple[str, str], frame: dict[str, Any], queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        session = None
        code = 1000
        try:
            session = self.manager.require_session(key[0])
            await self._serve_application(key, frame, queue, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            code = 1011
        finally:
            self.clients.pop(key, None)
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.send_json(
                        {"type": "task-stream.closed", "request_id": key[1], "code": code}
                    )

    async def _serve_application(
        self,
        key: tuple[str, str],
        frame: dict[str, Any],
        queue: asyncio.Queue[dict[str, Any]],
        session: Any,
    ) -> None:
        connection_id, request_id = key
        path = str(frame.get("path") or "")
        if not re.fullmatch(
            r"/api/project-tasks/(?:ws|[A-Za-z0-9_-]+/(?:browser-stream|terminal)/ws)", path
        ):
            await session.send_json(
                {"type": "task-stream.closed", "request_id": request_id, "code": 1008}
            )
            self.clients.pop(key, None)
            return
        token = await self.manager._owner_access_token(connection_id)
        query = frame.get("query") or {}
        if not isinstance(query, dict):
            query = {}
        # Never accept caller-supplied authentication or a generic profile/path.
        query = {str(k): str(v) for k, v in query.items() if k in _STREAM_QUERY_KEYS}
        query["token"] = token
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": urlencode(query).encode(),
            "headers": [
                (b"authorization", ("Bearer " + token).encode()),
                (b"host", b"bridge.local"),
            ],
            "client": ("127.0.0.1", 0),
            "server": ("bridge.local", 80),
            "subprotocols": [],
            "state": {},
        }
        connected = False

        async def receive() -> dict[str, Any]:
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            message = await queue.get()
            if not allows_task_stream_frame(path, message):
                return {"type": "websocket.disconnect", "code": 1008}
            return {"type": "websocket.receive", "text": json.dumps(message, ensure_ascii=False)}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "websocket.send":
                data = message.get("text")
                if data is None:
                    return
                payload = json.loads(data)
                # A peer owner JWT must never be returned to the local client.
                if isinstance(payload, dict) and payload.get("type") != "session":
                    await session.send_json(
                        {"type": "task-stream.server", "request_id": request_id, "frame": payload}
                    )
            elif message["type"] == "websocket.close":
                await session.send_json(
                    {
                        "type": "task-stream.closed",
                        "request_id": request_id,
                        "code": message.get("code", 1000),
                    }
                )

        await self.manager._asgi_app(scope, receive, send)

    async def relay(
        self, connection_id: str, owner_id: int, path: str, query: dict[str, str], websocket: Any
    ) -> None:
        await asyncio.to_thread(self.manager.get_owned, connection_id, owner_id)
        session = self.manager.require_session(connection_id)
        request_id = uuid4().hex
        key = (connection_id, request_id)
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self.waiters[key] = queue

        async def incoming() -> None:
            while True:
                frame = await websocket.receive_json()
                if isinstance(frame, dict) and frame.get("type") in {"authenticate", "session"}:
                    continue
                if not isinstance(frame, dict) or not allows_task_stream_frame(path, frame):
                    await websocket.close(code=1008)
                    return
                await asyncio.to_thread(self.manager.get_owned, connection_id, owner_id)
                await session.send_json(
                    {"type": "task-stream.client", "request_id": request_id, "frame": frame}
                )

        async def outgoing() -> None:
            while True:
                frame = await queue.get()
                await asyncio.to_thread(self.manager.get_owned, connection_id, owner_id)
                if frame["type"] == "task-stream.closed":
                    await websocket.close(code=int(frame.get("code") or 1000))
                    return
                await websocket.send_json(frame["frame"])

        tasks: list[asyncio.Task[None]] = []
        try:
            # Local authentication and unknown parameters must not cross the
            # Bridge, even though the receiving side also filters this query.
            query = {key: value for key, value in query.items() if key in _STREAM_QUERY_KEYS}
            await session.send_json(
                {"type": "task-stream.open", "request_id": request_id, "path": path, "query": query}
            )
            tasks = [
                asyncio.create_task(incoming()),
                asyncio.create_task(outgoing()),
                asyncio.create_task(session._closed.wait()),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.waiters.pop(key, None)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await session.send_json({"type": "task-stream.close", "request_id": request_id})

    async def disconnect(self, connection_id: str) -> None:
        runners = [task for (cid, _), task in self.runners.items() if cid == connection_id]
        for task in runners:
            task.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
        for (cid, _), queue in tuple(self.waiters.items()):
            if cid == connection_id:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({"type": "task-stream.closed", "code": 1012})
