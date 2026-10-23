"""Authenticated Bridge adapters for the existing unified Task protocol."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, Depends, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from octop.api.deps import current_user, get_server, resolve_user_from_token
from octop.infra.bridge.http_tunnel import MAX_TASK_TUNNEL_BODY_BYTES
from octop.infra.bridge.tasks import RemoteTasks
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/bridge/connections/{connection_id}")


class RemoteTaskCapabilities(BaseModel):
    host_id: str
    peer_identity: str = Field(description="Pinned remote installation and account identity.")
    available: bool
    protocol_version: int
    capabilities: dict[str, bool]
    max_request_bytes: int = Field(description="Maximum buffered Bridge request size in bytes.")


class RemoteTaskReference(BaseModel):
    host_id: str
    task_id: str
    summary: dict[str, Any] = Field(
        description="Cached public Task summary; contains no turns or credentials."
    )
    observed_at: int


class RemoteTaskReferences(BaseModel):
    host_id: str
    peer_identity: str | None
    items: list[RemoteTaskReference]
    cached: bool


def _remote(server: OctopServer) -> RemoteTasks:
    assert server.app_runtime is not None and server.services is not None
    return RemoteTasks(server.services, server.app_runtime.bridge_manager)


@router.get(
    "/task-capabilities", response_model=RemoteTaskCapabilities, summary="读取远端统一会话能力"
)
async def capabilities(
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await _remote(server).capabilities(user.id, connection_id)


@router.get(
    "/task-references", response_model=RemoteTaskReferences, summary="读取已观察的远端任务索引"
)
async def references(
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    remote = _remote(server)
    row = await asyncio.to_thread(remote.manager.get_owned, connection_id, user.id)
    assert server.services is not None
    fingerprint = await asyncio.to_thread(
        server.services.federated_task_repo.connection_fingerprint, user.id, connection_id
    )
    if fingerprint is not None and fingerprint != remote.connection_fingerprint(row):
        raise OctopError.localized(ErrorCode.BRIDGE_AUTH_FAILED, status=403)
    identity = await asyncio.to_thread(
        server.services.federated_task_repo.peer_identity, user.id, connection_id
    )
    rows = (
        await asyncio.to_thread(
            server.services.federated_task_repo.list, user.id, connection_id, identity
        )
        if identity
        else []
    )
    return {"host_id": connection_id, "peer_identity": identity, "items": rows, "cached": True}


async def proxy(
    connection_id: str,
    peer_path: str,
    request: Request,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    # Bound the buffer used by the Bridge JSON envelope, including multipart.
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_TASK_TUNNEL_BODY_BYTES:
            raise OctopError.localized(ErrorCode.TASK_ATTACHMENT_UNAVAILABLE, status=413)
        chunks.append(chunk)
    response = await _remote(server).proxy(
        user.id,
        connection_id,
        request.method,
        "/api/" + peer_path,
        request.url.query,
        dict(request.headers),
        b"".join(chunks),
    )
    return Response(
        response.content, status_code=response.status_code, headers=dict(response.headers)
    )


for _proxy_method in ("GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"):
    router.add_api_route(
        "/task-api/{peer_path:path}",
        proxy,
        methods=[_proxy_method],
        operation_id="remote_task_proxy_" + _proxy_method.lower(),
        summary="转发已允许的远端项目和任务操作",
        description="Requires ownership and a matching remote Task capability/identity handshake. Only product paths in the method allowlist are forwarded as the peer account. Execution remains on the peer; credentials and peer session tokens never reach the browser. Bodies are limited to 16 MiB per request or response; oversized transfers return 413 without disconnecting other streams.",
        responses={
            200: {
                "description": "Peer JSON metadata or binary content.",
                "content": {
                    "application/json": {"schema": {"type": "object"}},
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
                },
            }
        },
    )


async def _relay(
    websocket: WebSocket, connection_id: str, task_id: str | None = None, surface: str = "main"
) -> None:
    server = websocket.app.state.octop_server
    token = websocket.headers.get("authorization", "").removeprefix(
        "Bearer "
    ).strip() or websocket.query_params.get("token")
    accepted = False
    if not token:
        await websocket.accept()
        accepted = True
        try:
            frame = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            token = (
                frame.get("token")
                if isinstance(frame, dict) and frame.get("type") == "authenticate"
                else None
            )
        except (TimeoutError, ValueError, WebSocketDisconnect):
            token = None
    if not isinstance(token, str) or not token or len(token) > 16384:
        await websocket.close(code=1008)
        return
    try:
        user = await asyncio.to_thread(resolve_user_from_token, server, token)
    except OctopError:
        await websocket.close(code=1008)
        return
    auth_token = str(token)
    remote = _remote(server)
    try:
        await remote.capabilities(user.id, connection_id)
    except OctopError:
        await websocket.close(code=1008)
        return
    if not accepted:
        await websocket.accept()
    path = (
        "/api/project-tasks/ws" if task_id is None else f"/api/project-tasks/{task_id}/{surface}/ws"
    )

    async def auth_watch() -> None:
        while True:
            await asyncio.sleep(10)
            try:
                current = await asyncio.to_thread(resolve_user_from_token, server, auth_token)
                if current.id != user.id:
                    raise OctopError.localized(ErrorCode.AUTH_FAILED)
                await asyncio.to_thread(remote.manager.get_owned, connection_id, user.id)
            except OctopError:
                await websocket.close(code=1008)
                return

    runner = asyncio.create_task(
        remote.manager.task_streams.relay(
            connection_id, user.id, path, dict(websocket.query_params), websocket
        )
    )
    watch = asyncio.create_task(auth_watch())
    try:
        await asyncio.wait([runner, watch], return_when=asyncio.FIRST_COMPLETED)
    finally:
        runner.cancel()
        watch.cancel()
        await asyncio.gather(runner, watch, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()


@router.websocket("/project-tasks/ws")
async def task_socket(websocket: WebSocket, connection_id: str) -> None:
    await _relay(websocket, connection_id)


@router.websocket("/project-tasks/{task_id}/browser-stream/ws")
async def browser_socket(websocket: WebSocket, connection_id: str, task_id: str) -> None:
    await _relay(websocket, connection_id, task_id, "browser-stream")


@router.websocket("/project-tasks/{task_id}/terminal/ws")
async def terminal_socket(websocket: WebSocket, connection_id: str, task_id: str) -> None:
    await _relay(websocket, connection_id, task_id, "terminal")
