"""Typed, owner-scoped Bridge relay for AgentComputer independent projects."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError

from octop.api.agentcomputer import (
    API_PREFIX,
    PROJECT_TASK_ERROR_CODES,
    ProtocolErrorEnvelope,
)
from octop.api.deps import current_bearer_user, get_server
from octop.api.routers.bridge import _bridge
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import tasks
from octop.infra.projects.discovery import ProjectTaskCapabilities
from octop.infra.projects.models import (
    ProjectFields,
    ProjectPatch,
    ProjectResources,
    ProjectUpdate,
    ProjectView,
)
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

router = APIRouter(
    prefix="/bridge/connections/{connection_id}/agentcomputer/v1",
    responses={
        status: {"model": ProtocolErrorEnvelope}
        for status in (401, 403, 404, 409, 413, 422, 502, 503)
    },
)


async def _relay(
    request: Request,
    server: OctopServer,
    user: User,
    connection_id: str,
    resource: str,
    response_type: Any,
) -> JSONResponse:
    headers = None
    body = None
    if request.method in {"POST", "PATCH", "PUT"}:
        headers = {"content-type": "application/json"}
        if request.method == "POST" and resource == "/projects":
            headers["idempotency-key"] = request.headers["idempotency-key"]
        body = await request.body()
    try:
        peer = await _bridge(server).tunnel_http(
            connection_id=connection_id,
            owner_user_id=user.id,
            method=request.method,
            path=API_PREFIX + resource,
            headers=headers,
            body=body,
        )
    except OctopError as exc:
        if exc.code == ErrorCode.BRIDGE_TUNNEL_FAILED:
            raise OctopError(ErrorCode.BRIDGE_TUNNEL_FAILED, "peer request failed") from exc
        raise
    if 200 <= peer.status_code < 300:
        try:
            adapter: TypeAdapter[Any] = TypeAdapter(response_type)
            payload = adapter.dump_python(adapter.validate_python(peer.json()), mode="json")
        except (ValueError, ValidationError) as exc:
            raise OctopError(
                ErrorCode.BRIDGE_TUNNEL_FAILED, "invalid peer project/task response"
            ) from exc
    else:
        try:
            envelope = ProtocolErrorEnvelope.model_validate(peer.json())
            payload = envelope.model_dump(mode="json", exclude_none=True)
            safe_reasons = {code.value for code in PROJECT_TASK_ERROR_CODES} | {"setupRequired"}
            if envelope.error.reasonCode not in safe_reasons:
                payload["error"].pop("reasonCode", None)
        except (ValueError, ValidationError):
            payload = {
                "error": {
                    "code": "resourceNotFound" if peer.status_code == 404 else "internalError",
                    "retryable": peer.status_code >= 500,
                }
            }
    return JSONResponse(
        status_code=peer.status_code, content=payload, headers={"Cache-Control": "no-store"}
    )


@router.get(
    "/capabilities",
    response_model=ProjectTaskCapabilities,
    status_code=200,
    summary="读取远端 AgentComputer 协议能力",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def capabilities(
    request: Request,
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, "/capabilities", ProjectTaskCapabilities
    )


@router.get(
    "/projects",
    response_model=list[ProjectView],
    status_code=200,
    summary="列出远端独立项目",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def list_projects(
    request: Request,
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(request, server, user, connection_id, "/projects", list[ProjectView])


@router.get(
    "/projects/resources",
    response_model=ProjectResources,
    status_code=200,
    summary="读取远端项目可选资源",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def resources(
    request: Request,
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, "/projects/resources", ProjectResources
    )


@router.post(
    "/projects",
    response_model=ProjectView,
    status_code=201,
    summary="创建远端独立项目",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def create_project(
    request: Request,
    connection_id: str,
    body: ProjectFields,
    idempotency_key: str = Header(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="转发到远端的创建幂等标识；重试复用。",
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(request, server, user, connection_id, "/projects", ProjectView)


@router.get(
    "/projects/{project_id}",
    response_model=ProjectView,
    status_code=200,
    summary="读取远端项目配置",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def project_detail(
    request: Request,
    connection_id: str,
    project_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/projects/{project_id}", ProjectView
    )


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectView,
    status_code=200,
    summary="局部修改远端项目",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def patch_project(
    request: Request,
    connection_id: str,
    project_id: str,
    body: ProjectPatch,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/projects/{project_id}", ProjectView
    )


@router.put(
    "/projects/{project_id}",
    response_model=ProjectView,
    status_code=200,
    summary="完整替换远端项目配置",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def update_project(
    request: Request,
    connection_id: str,
    project_id: str,
    body: ProjectUpdate,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/projects/{project_id}", ProjectView
    )


@router.get(
    "/projects/{project_id}/tasks",
    response_model=list[tasks.TaskSummary],
    status_code=200,
    summary="列出远端项目任务",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def project_tasks(
    request: Request,
    connection_id: str,
    project_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request,
        server,
        user,
        connection_id,
        f"/projects/{project_id}/tasks",
        list[tasks.TaskSummary],
    )


@router.post(
    "/projects/{project_id}/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="在远端项目中发送首条消息并创建任务",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def create_project_task(
    request: Request,
    connection_id: str,
    project_id: str,
    body: tasks.TaskMessage,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/projects/{project_id}/tasks", tasks.TaskDetail
    )


@router.get(
    "/tasks",
    response_model=list[tasks.TaskSummary],
    status_code=200,
    summary="列出远端无项目任务",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def standalone_tasks(
    request: Request,
    connection_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(request, server, user, connection_id, "/tasks", list[tasks.TaskSummary])


@router.post(
    "/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="创建远端任务并发送首条消息",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def create_task(
    request: Request,
    connection_id: str,
    body: tasks.TaskCreate,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(request, server, user, connection_id, "/tasks", tasks.TaskDetail)


@router.get(
    "/tasks/{task_id}",
    response_model=tasks.TaskDetail,
    status_code=200,
    summary="读取远端任务与增量回复",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def task_detail(
    request: Request,
    connection_id: str,
    task_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(request, server, user, connection_id, f"/tasks/{task_id}", tasks.TaskDetail)


@router.get(
    "/tasks/{task_id}/turns",
    response_model=list[tasks.TaskTurn],
    status_code=200,
    summary="读取远端多轮任务历史",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def task_turns(
    request: Request,
    connection_id: str,
    task_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/tasks/{task_id}/turns", list[tasks.TaskTurn]
    )


@router.post(
    "/tasks/{task_id}/turns",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="继续远端同一任务对话",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def send_turn(
    request: Request,
    connection_id: str,
    task_id: str,
    body: tasks.TaskMessage,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/tasks/{task_id}/turns", tasks.TaskDetail
    )


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=tasks.TaskDetail,
    status_code=200,
    summary="停止远端当前任务回复",
    description="仅转发到当前用户拥有的 Bridge 连接；远端仍按连接账号授权。使用 AgentComputer 独立项目/多轮任务契约，不转发调用方凭据或任意路径。",
)
async def cancel_task(
    request: Request,
    connection_id: str,
    task_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> JSONResponse:
    return await _relay(
        request, server, user, connection_id, f"/tasks/{task_id}/cancel", tasks.TaskDetail
    )
