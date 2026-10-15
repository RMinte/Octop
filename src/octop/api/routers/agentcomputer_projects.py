"""Dedicated AgentComputer protocol backed by independent projects and tasks."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse

from octop.api.agentcomputer import ProtocolErrorEnvelope, protocol_error_response
from octop.api.deps import current_bearer_user, get_server, require_database
from octop.infra.agentcomputer.identity import IdentityError
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import service, sync, tasks
from octop.infra.projects.discovery import ProjectTaskCapabilities, get_capabilities
from octop.infra.projects.models import (
    ProjectFields,
    ProjectPatch,
    ProjectResources,
    ProjectUpdate,
    ProjectView,
)
from octop.infra.projects.runtime import ProjectTaskWorker
from octop.infra.server import OctopServer
from octop.infra.users.identity import User

logger = logging.getLogger(__name__)
router = APIRouter(
    responses={
        status: {"model": ProtocolErrorEnvelope} for status in (401, 403, 404, 409, 413, 422, 503)
    }
)


def _services(server: OctopServer, response: Response) -> SharedServices:
    response.headers["Cache-Control"] = "no-store"
    require_database(server)
    assert server.services is not None
    return server.services


def _worker(server: OctopServer) -> ProjectTaskWorker:
    worker = server.project_task_worker
    if worker is None or worker.closing:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)
    return worker


@router.get(
    "/capabilities",
    response_model=ProjectTaskCapabilities,
    summary="读取 AgentComputer 独立项目与任务协议能力",
    description="专供 AgentComputer 的版本化入口。contract=independent-project-tasks；能力仅描述当前独立项目、多轮会话与轮询回复，不开放旧专家绑定、Run、事件、文档或命令回执协议。executionAvailable 反映当前模型与 worker 状态，实际发送还会校验本人配额和项目资源。",
)
async def capabilities(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectTaskCapabilities | JSONResponse:
    services = _services(server, response)
    worker = server.project_task_worker
    try:
        return await server.agentcomputer_http_operations.run_thread(
            get_capabilities, services, worker_ready=worker is not None and not worker.closing
        )
    except (IdentityError, OSError):
        logger.exception("AgentComputer installation identity is unavailable")
        return protocol_error_response("storageUnavailable", 503)


@router.get(
    "/projects",
    response_model=list[ProjectView],
    summary="列出 AgentComputer 项目",
    description="返回本人独立项目，与 Octop 网页共享同一份数据；按最近更新排序。列表是数组，不是旧 items/nextCursor。",
)
async def list_projects(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> list[ProjectView]:
    return await server.agentcomputer_http_operations.run_thread(
        service.list_projects, _services(server, response), user.id
    )


@router.get(
    "/projects/resources",
    response_model=ProjectResources,
    summary="读取 AgentComputer 项目可选资源",
    description="返回服务端目录范围、已安装 Skill 和本人 MCP 引用，不返回凭据，也不访问客户端目录。",
)
async def resources(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectResources:
    return await server.agentcomputer_http_operations.run_thread(
        service.resources, _services(server, response), user.id
    )


@router.post(
    "/projects",
    response_model=ProjectView,
    status_code=201,
    summary="新建 AgentComputer 独立项目",
    description="Idempotency-Key 必填，同用户同标识同内容重试返回首次创建结果。只保存项目配置，不创建或绑定专家；客户端目录仅保存路径。",
)
async def create_project(
    body: ProjectFields,
    response: Response,
    idempotency_key: str = Header(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="本次创建标识；网络重试复用相同标识。",
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectView:
    return await server.agentcomputer_http_operations.run_thread(
        service.create_project, _services(server, response), user.id, body, idempotency_key
    )


@router.get(
    "/projects/sync",
    response_model=sync.ProjectSyncPage,
    summary="同步项目最终状态",
    description="不带同步游标返回完整项目快照；之后只返回变过的项目，游标与任务独立。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_projects(
    response: Response,
    sync_cursor: str | None = Query(
        default=None, max_length=4096, description="已有项目同步位置；首次可省略。"
    ),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> sync.ProjectSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await server.agentcomputer_http_operations.run_thread(
        sync.project_sync,
        _services(server, response),
        user.id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/projects/{project_id}/tasks/sync",
    response_model=sync.TaskSyncPage,
    summary="同步任务列表最终状态",
    description="只返回该列表基线之后变化的任务摘要，同一任务合并为最终状态，不包含正文。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_project_tasks(
    project_id: str,
    response: Response,
    sync_cursor: str = Query(min_length=1, max_length=4096, description="任务首页返回的增量基线。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await server.agentcomputer_http_operations.run_thread(
        sync.task_sync,
        _services(server, response),
        user.id,
        project_id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/projects/{project_id}",
    response_model=ProjectView,
    summary="读取 AgentComputer 项目配置",
    description="使用不透明字符串项目 ID，返回完整配置和 revision；不存在或其他用户的项目均为 404。",
)
async def project_detail(
    project_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectView:
    return await server.agentcomputer_http_operations.run_thread(
        service.get_project, _services(server, response), user.id, project_id
    )


@router.patch(
    "/projects/{project_id}",
    response_model=ProjectView,
    summary="局部修改 AgentComputer 项目",
    description="必须携带 expected_revision；只修改提交字段，版本冲突返回 409。与网页立即看到同一份配置，不修改专家。",
)
async def patch_project(
    project_id: str,
    body: ProjectPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectView:
    return await server.agentcomputer_http_operations.run_thread(
        service.patch_project, _services(server, response), user.id, project_id, body
    )


@router.put(
    "/projects/{project_id}",
    response_model=ProjectView,
    summary="完整替换 AgentComputer 项目配置",
    description="必须显式提交所有项目配置字段与 expected_revision；普通编辑推荐 PATCH。",
)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> ProjectView:
    return await server.agentcomputer_http_operations.run_thread(
        service.update_project, _services(server, response), user.id, project_id, body
    )


@router.get(
    "/projects/{project_id}/tasks",
    response_model=sync.TaskListPage,
    summary="列出 AgentComputer 项目任务",
    description="按最近活动位置分页返回本人项目任务摘要；默认 50 条，仅首页返回独立同步基线。",
)
async def project_tasks(
    project_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> sync.TaskListPage:
    return await server.agentcomputer_http_operations.run_thread(
        sync.task_page,
        _services(server, response),
        user.id,
        project_id,
        limit=limit,
        page_cursor=page_cursor,
    )


@router.post(
    "/projects/{project_id}/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="在 AgentComputer 项目中发送首条消息并创建任务",
    description="原子保存首条消息和任务，request_id 幂等重试。每轮采用发送时项目配置，不创建专家。",
)
async def create_project_task(
    project_id: str,
    body: tasks.TaskMessage,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> tasks.TaskDetail:
    worker = _worker(server)
    result = await server.agentcomputer_http_operations.run_thread(
        tasks.send, _services(server, response), user.id, body, project_id=project_id
    )
    worker.wake()
    return result


@router.get(
    "/tasks",
    response_model=sync.TaskListPage,
    summary="列出 AgentComputer 无项目任务",
    description="按最近活动位置分页读取本人无项目任务摘要；默认 50 条，不包含正文。",
)
async def standalone_tasks(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> sync.TaskListPage:
    return await server.agentcomputer_http_operations.run_thread(
        sync.task_page,
        _services(server, response),
        user.id,
        None,
        limit=limit,
        page_cursor=page_cursor,
    )


@router.post(
    "/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="创建 AgentComputer 任务并发送首条消息",
    description="project_id 可省略或为 null，创建无项目任务；也可指定本人项目。request_id 与相同消息重试不重复执行。",
)
async def create_task(
    body: tasks.TaskCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> tasks.TaskDetail:
    worker = _worker(server)
    result = await server.agentcomputer_http_operations.run_thread(
        tasks.send, _services(server, response), user.id, body, project_id=body.project_id
    )
    worker.wake()
    return result


@router.get(
    "/tasks/sync",
    response_model=sync.TaskSyncPage,
    summary="同步任务列表最终状态",
    description="只返回该列表基线之后变化的任务摘要，同一任务合并为最终状态，不包含正文。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_standalone_tasks(
    response: Response,
    sync_cursor: str = Query(min_length=1, max_length=4096, description="任务首页返回的增量基线。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="本批变更下一页游标。"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await server.agentcomputer_http_operations.run_thread(
        sync.task_sync,
        _services(server, response),
        user.id,
        None,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/tasks/{task_id}",
    response_model=tasks.TaskDetail,
    summary="读取 AgentComputer 任务、历史与增量回复",
    description="轮询读取全部轮次、已保存正文、工具名称与执行状态。不提供旧 Run 身份、事件 cursor 或完成证据协议。其他用户的任务返回 404。",
)
async def task_detail(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> tasks.TaskDetail:
    return await server.agentcomputer_http_operations.run_thread(
        tasks.detail, _services(server, response), user.id, task_id
    )


@router.get(
    "/tasks/{task_id}/turns",
    response_model=list[tasks.TaskTurn],
    summary="读取 AgentComputer 多轮任务历史",
    description="返回同一任务已保存的全部轮次；与任务详情中的 turns 相同，不使用旧分页 cursor。",
)
async def task_turns(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> list[tasks.TaskTurn]:
    result = await server.agentcomputer_http_operations.run_thread(
        tasks.detail, _services(server, response), user.id, task_id
    )
    return result.turns


@router.post(
    "/tasks/{task_id}/turns",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="继续 AgentComputer 同一任务的下一轮对话",
    description="沿用相同 task_id，发送 request_id 和 prompt。采用最新项目配置；同标识同消息幂等，任务忙时返回 409。",
)
async def send_turn(
    task_id: str,
    body: tasks.TaskMessage,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> tasks.TaskDetail:
    worker = _worker(server)
    result = await server.agentcomputer_http_operations.run_thread(
        tasks.send, _services(server, response), user.id, body, task_id=task_id
    )
    worker.wake()
    return result


@router.post(
    "/tasks/{task_id}/cancel",
    response_model=tasks.TaskDetail,
    summary="停止 AgentComputer 当前任务回复",
    description="取消排队或请求停止运行中的回复，保留历史，之后可在同一任务继续对话。取消不回滚已执行工具的副作用。",
)
async def cancel_task(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_bearer_user),
) -> tasks.TaskDetail:
    return await server.agentcomputer_http_operations.run_thread(
        tasks.cancel, _services(server, response), user.id, task_id
    )
