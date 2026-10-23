"""Authenticated Task workbench transports; domain scope never uses expert directories."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import Field
from starlette.websockets import WebSocketState

from octop.api.common.content_disposition import content_disposition
from octop.api.common.upload_limit import read_upload_capped
from octop.api.deps import current_user, get_server, require_database, resolve_user_from_token
from octop.api.routers import terminal
from octop.api.routers.browser.harness import harness_sessions_payload
from octop.api.routers.browser.stream import run_browser_stream_session
from octop.infra.browser.sessions import CONTROL_OWNERS
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import workbench, workbench_browser
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(prefix="/project-tasks/{task_id}")


async def _scope(
    server: OctopServer,
    user: User,
    task_id: str,
    expected: str | None = None,
    *,
    mutable: bool = False,
    permission: str | None = None,
) -> workbench.WorkbenchScope:
    require_database(server)
    assert server.services is not None
    return await asyncio.to_thread(
        workbench.resolve,
        server.services,
        user.id,
        task_id,
        expected_scope_revision=expected,
        mutable=mutable,
        permission=permission,
    )


@router.get("/workbench", response_model=workbench.WorkbenchView, summary="读取当前任务工作台范围")
async def context(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> workbench.WorkbenchView:
    response.headers["Cache-Control"] = "no-store"
    return (await _scope(server, user, task_id)).view


@router.get(
    "/workspace/tree-children",
    response_model=list[workbench.WorkspaceEntry],
    summary="列出任务工作目录",
)
async def tree(
    task_id: str,
    path: str = ".",
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> list[workbench.WorkspaceEntry]:
    return await workbench.tree(await _scope(server, user, task_id, expected_scope_revision), path)


@router.get(
    "/workspace/file", response_model=workbench.WorkspaceFile, summary="读取任务工作区文本文件"
)
async def read_file(
    task_id: str,
    path: str,
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> workbench.WorkspaceFile:
    return await workbench.read(await _scope(server, user, task_id, expected_scope_revision), path)


@router.put(
    "/workspace/file", response_model=workbench.WorkspaceFile, summary="保存任务工作区文本文件"
)
async def write_file(
    task_id: str,
    body: workbench.FileWrite,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> workbench.WorkspaceFile:
    return await workbench.write(
        await _scope(server, user, task_id, body.expected_scope_revision, mutable=True), body
    )


@router.post("/workspace/mkdir", summary="创建任务工作区目录")
async def mkdir(
    task_id: str,
    body: workbench.PathRequest,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    scope = await _scope(server, user, task_id, body.expected_scope_revision, mutable=True)
    path = await workbench.public_path(scope, body.path, mutable=True)
    await workbench.workspace_io(scope.workspace.amkdir(path))
    return {"path": path, "is_dir": True}


@router.post("/workspace/move", summary="移动任务工作区文件")
async def move(
    task_id: str,
    body: workbench.FileMove,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    scope = await _scope(server, user, task_id, body.expected_scope_revision, mutable=True)
    source, target = (
        await workbench.public_path(scope, body.path, mutable=True),
        await workbench.public_path(scope, body.destination, mutable=True),
    )
    await workbench.workspace_io(scope.workspace.amove(source, target))
    return {"path": target}


@router.delete(
    "/workspace/file", status_code=204, response_class=Response, summary="删除任务工作区文件"
)
async def delete_file(
    task_id: str,
    path: str,
    expected_scope_revision: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    scope = await _scope(server, user, task_id, expected_scope_revision, mutable=True)
    await workbench.workspace_io(
        scope.workspace.adelete(await workbench.public_path(scope, path, mutable=True))
    )
    return Response(status_code=204)


@router.post("/workspace/upload", summary="上传文件到任务工作区")
async def upload(
    task_id: str,
    path: str,
    file: UploadFile = File(...),
    expected_scope_revision: str = Form(...),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    scope = await _scope(server, user, task_id, expected_scope_revision, mutable=True)
    target = await workbench.public_path(scope, path, mutable=True)
    assert server.services is not None
    data = await read_upload_capped(file, max_bytes=server.services.config.max_upload_bytes)
    await workbench.workspace_io(scope.workspace.aupload_bytes(target, data))
    return {"path": target, "size": len(data)}


@router.get("/workspace/media", summary="预览任务工作区媒体文件", response_class=Response)
@router.get("/workspace/download-file", summary="下载任务工作区文件", response_class=Response)
async def download(
    task_id: str,
    path: str,
    request: Request,
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    data, media_type, name = await workbench.download(
        await _scope(server, user, task_id, expected_scope_revision), path
    )
    disposition = "attachment" if request.url.path.endswith("download-file") else "inline"
    headers = {
        "Cache-Control": "no-store",
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": content_disposition(name, disposition=disposition),
    }
    if media_type in {"text/html", "image/svg+xml", "application/xhtml+xml"}:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    selected = request.headers.get("range")
    if selected and selected.startswith("bytes=") and "," not in selected:
        try:
            lower, upper = selected[6:].split("-", 1)
            begin = int(lower) if lower else max(0, len(data) - int(upper))
            end = min(int(upper), len(data) - 1) if lower and upper else len(data) - 1
            if begin < 0 or begin > end or begin >= len(data):
                raise ValueError
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{len(data)}"})
        headers["Content-Range"] = f"bytes {begin}-{end}/{len(data)}"
        return Response(
            data[begin : end + 1], status_code=206, media_type=media_type, headers=headers
        )
    return Response(data, media_type=media_type, headers=headers)


@router.get("/browser/sessions", summary="读取当前任务的浏览器会话")
async def browser_sessions(
    task_id: str,
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    scope = await _scope(server, user, task_id, expected_scope_revision, permission="browser")
    return await harness_sessions_payload(scope.browser_profile)


@router.post("/browser/handoff", summary="切换当前任务浏览器控制权")
async def browser_handoff(
    task_id: str,
    body: workbench_browser.BrowserHandoff,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    scope = await _scope(
        server, user, task_id, body.expected_scope_revision, mutable=True, permission="browser"
    )
    CONTROL_OWNERS[scope.browser_profile] = body.target
    payload = await harness_sessions_payload(scope.browser_profile)
    session = next(iter(payload.get("sessions", [])), None) or {
        "session_id": scope.browser_profile,
        "profile_name": scope.browser_profile,
        "conversation_id": task_id,
        "channel_source": "dashboard",
        "state": "idle",
        "current_url": "",
        "created_at": 0,
        "last_activity_at": 0,
    }
    session["control_owner"] = body.target
    return {"ok": True, "session": session}


async def close_task_sessions(task_id: str, user_id: int) -> None:
    for key, session in list(terminal._sessions.items()):
        if key[0].startswith(f"task:{task_id}:") and session.user_id == user_id:
            await terminal._destroy_session(session, cancel_pump=True)
    await workbench_browser.close_task(task_id)


@router.websocket("/browser-stream/ws")
async def browser_stream(
    websocket: WebSocket,
    task_id: str,
    token: str | None = None,
    expected_scope_revision: str | None = None,
    listen_only: int = 0,
    width: int = 1280,
    height: int = 800,
) -> None:
    server = websocket.app.state.octop_server
    try:
        user = resolve_user_from_token(server, token or "")
        if not listen_only and not expected_scope_revision:
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        scope = await _scope(
            server,
            user,
            task_id,
            expected_scope_revision,
            mutable=not listen_only,
            permission="browser",
        )
    except OctopError:
        await websocket.close(code=4003)
        return
    await websocket.accept()

    async def receive() -> str:
        raw = await websocket.receive_text()
        fresh = resolve_user_from_token(server, token or "")
        await _scope(
            server,
            fresh,
            task_id,
            scope.view.scope_revision,
            mutable=not listen_only,
            permission="browser",
        )
        message = json.loads(raw)
        if message.get("url") and not workbench_browser.public_url(str(message["url"])):
            raise OctopError.localized(ErrorCode.FORBIDDEN)
        return raw

    async def send(payload: dict[str, Any]) -> None:
        fresh = resolve_user_from_token(server, token or "")
        await _scope(
            server,
            fresh,
            task_id,
            scope.view.scope_revision,
            mutable=not listen_only,
            permission="browser",
        )
        await websocket.send_json(payload)

    try:
        await run_browser_stream_session(
            send_json=send,
            is_connected=lambda: websocket.application_state == WebSocketState.CONNECTED,
            receive_text=receive,
            user_id=user.id,
            listen_only=bool(listen_only),
            default_width=width,
            default_height=height,
            profile_name=scope.browser_profile,
            screenshots_dir=Path(scope.view.server_directory) / "outbound" / "screenshots",
        )
    except (WebSocketDisconnect, OctopError):
        pass
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


@router.websocket("/terminal/ws")
async def terminal_stream(
    websocket: WebSocket,
    task_id: str,
    token: str | None = None,
    expected_scope_revision: str | None = None,
    session_id: str | None = None,
    cols: int = 80,
    rows: int = 24,
) -> None:
    server = websocket.app.state.octop_server
    try:
        user = resolve_user_from_token(server, token or "")
        if not expected_scope_revision:
            raise OctopError.localized(ErrorCode.TASK_REVISION_CONFLICT)
        scope = await _scope(
            server, user, task_id, expected_scope_revision, mutable=True, permission="terminal"
        )
    except OctopError:
        await websocket.close(code=4003)
        return

    async def validate() -> None:
        fresh = resolve_user_from_token(server, token or "")
        await _scope(
            server, fresh, task_id, scope.view.scope_revision, mutable=True, permission="terminal"
        )

    async def watch_access() -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await validate()
            except OctopError:
                for key, session in list(terminal._sessions.items()):
                    if key[0] == f"task:{task_id}:{scope.view.scope_revision}":
                        await terminal._destroy_session(session, cancel_pump=True)
                with contextlib.suppress(Exception):
                    await websocket.close(code=4003)
                return

    watcher = asyncio.create_task(watch_access())
    try:
        await terminal.run_terminal_session(
            websocket,
            f"task:{task_id}:{scope.view.scope_revision}",
            user,
            scope.view.server_directory,
            session_id,
            max(1, min(cols, 1000)),
            max(1, min(rows, 1000)),
            validate=validate,
        )
    except (OctopError, WebSocketDisconnect):
        with contextlib.suppress(Exception):
            await websocket.close(code=4003)
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@router.delete(
    "/terminal/sessions/{session_id}",
    status_code=204,
    response_class=Response,
    summary="结束任务终端会话",
)
async def terminal_close(
    task_id: str,
    session_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    await _scope(server, user, task_id, permission="terminal")
    for key, session in list(terminal._sessions.items()):
        if (
            key[0].startswith(f"task:{task_id}:")
            and key[1] == session_id
            and session.user_id == user.id
        ):
            await terminal._destroy_session(session, cancel_pump=True)
    return Response(status_code=204)


@router.get("/browser/record-replay/status", summary="读取任务浏览器录制状态")
async def recording_status(
    task_id: str,
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await workbench_browser.recording_status(
        await _scope(server, user, task_id, expected_scope_revision, permission="browser")
    )


@router.get("/browser/record-replay/skill-content", summary="恢复当前任务录制生成的技能草稿")
async def recording_content(
    task_id: str,
    recordingId: str,
    expected_scope_revision: str | None = None,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await workbench_browser.recording_content(
        await _scope(server, user, task_id, expected_scope_revision, permission="browser"),
        recordingId,
    )


@router.post("/browser/record-replay/start", summary="开始当前任务的浏览器录制")
async def recording_start(
    task_id: str,
    body: workbench_browser.RecordingStart,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await workbench_browser.start_recording(
        await _scope(
            server, user, task_id, body.expected_scope_revision, mutable=True, permission="browser"
        ),
        body,
    )


@router.post("/browser/record-replay/stop-and-generate-skill", summary="结束任务录制并生成技能草稿")
async def recording_stop(
    task_id: str,
    body: workbench_browser.RecordingStop,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await workbench_browser.stop_recording(
        await _scope(
            server, user, task_id, body.expected_scope_revision, mutable=True, permission="browser"
        ),
        body,
    )


@router.post("/recordings/{recording_id}/apply", summary="明确将录制技能应用到当前任务工作区")
async def recording_apply(
    task_id: str,
    recording_id: str,
    body: workbench_browser.RecordingApply,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> dict[str, Any]:
    return await workbench_browser.apply_recording(
        await _scope(
            server, user, task_id, body.expected_scope_revision, mutable=True, permission="browser"
        ),
        recording_id,
        body,
    )


class ToolUiRequest(workbench.ScopeRequest):
    path: str = Field(min_length=1, max_length=4096)
    method: Literal["GET", "PUT", "POST", "DELETE"] = "GET"
    body: dict[str, Any] = Field(default_factory=dict)


@router.post("/tool-ui/{turn_id}/{call_id}/request", summary="执行当前任务工具 UI 的范围内请求")
async def tool_ui_request(
    task_id: str,
    turn_id: str,
    call_id: str,
    body: ToolUiRequest,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Any:
    require_database(server)
    assert server.services is not None
    return await workbench.tool_request(
        server.services,
        user.id,
        task_id,
        turn_id,
        call_id,
        path=body.path,
        method=body.method,
        body=body.body,
        expected_scope_revision=body.expected_scope_revision,
    )
