"""HTTP surface for independent projects."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Header, Query, Response

from octop.api.deps import current_user, get_server, require_database
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import service, sync, tasks
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

router = APIRouter(prefix="/projects")


def _services(server: OctopServer) -> SharedServices:
    require_database(server)
    assert server.services is not None
    return server.services


@router.get(
    "",
    response_model=list[ProjectView],
    summary="列出我的项目",
    description="每次读取当前服务端的最新项目。按最近更新排序，只返回本人数据；无需专家、模型或 Bridge。",
)
async def list_projects(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> list[ProjectView]:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.list_projects, _services(server), user.id)


@router.get(
    "/resources",
    response_model=ProjectResources,
    summary="列出项目可关联的目录、Skill 和 MCP",
    description="返回共享技能库和本人有权使用的 MCP 连接。只返回引用和配置状态，不返回密钥，不连接 MCP。",
)
async def project_resources(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectResources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.resources, _services(server), user.id)


@router.post(
    "",
    response_model=ProjectView,
    status_code=201,
    summary="新建独立项目",
    description="Octop 网页的独立项目入口；AgentComputer 使用 /api/agentcomputer/v1/projects，共享底层服务与数据。必须提供 Idempotency-Key 请求头；同一用户、同一标识和相同内容重试返回首次创建结果，内容不同返回 409。保存两类目录、多个 Skill/MCP 引用和行动指南；不创建专家，不上传或访问客户端文件。同名项目返回 409。",
)
async def create_project(
    body: ProjectFields,
    response: Response,
    idempotency_key: str = Header(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="本次创建的唯一标识；断网重试必须复用，新的创建使用新标识。",
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.create_project, _services(server), user.id, body, idempotency_key
    )


@router.get(
    "/sync",
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
    user: User = Depends(current_user),
) -> sync.ProjectSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.project_sync,
        _services(server),
        user.id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/{project_id}/tasks/sync",
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
    user: User = Depends(current_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_sync,
        _services(server),
        user.id,
        project_id,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@router.get(
    "/{project_id}",
    response_model=ProjectView,
    summary="读取项目详情",
    description="返回该项目自己的完整配置和版本。其他用户的项目返回 404。",
)
async def get_project(
    project_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(service.get_project, _services(server), user.id, project_id)


@router.patch(
    "/{project_id}",
    response_model=ProjectView,
    summary="局部修改项目",
    description="Octop 网页的项目编辑入口。只修改提交的字段；省略保持原值，目录传 null 解除关联，Skill/MCP 传 [] 清空。必须提交 expected_revision；版本冲突返回 409。名称、简介、行动指南、Skill/MCP 不接受 null。",
)
async def patch_project(
    project_id: str,
    body: ProjectPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.patch_project, _services(server), user.id, project_id, body
    )


@router.put(
    "/{project_id}",
    response_model=ProjectView,
    summary="修改项目",
    description="完整替换项目字段，保存后 revision 加一；expected_revision 不匹配返回 409。"
    "只修改项目配置，不修改专家或目录内容；客户端路径不会在服务端解析、检查或访问。",
)
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> ProjectView:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        service.update_project, _services(server), user.id, project_id, body
    )


@router.get(
    "/{project_id}/tasks",
    response_model=sync.TaskListPage,
    summary="列出项目任务",
    description="按最近活动位置分页，默认 50 条。仅首页返回独立同步基线，不读取任务正文。",
)
async def list_project_tasks(
    project_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page, _services(server), user.id, project_id, limit=limit, page_cursor=page_cursor
    )


@router.post(
    "/{project_id}/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="在项目中发送首条消息并创建任务",
    description="保存任务及首条消息后排队执行，返回任务详情。每轮采用发送时的项目配置；不创建专家。request_id 用于幂等重试。模型或资源未配置时不会创建空任务。",
)
async def create_project_task(
    project_id: str,
    body: tasks.TaskMessage,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await asyncio.to_thread(
        tasks.send, _services(server), user.id, body, project_id=project_id
    )
    worker.wake()
    return result


def _task_worker(server: OctopServer) -> ProjectTaskWorker:
    worker = server.project_task_worker
    if worker is None or worker.closing:
        raise OctopError.localized(ErrorCode.CONTROL_PLANE_BUSY)
    return worker


task_router = APIRouter(prefix="/project-tasks")


@task_router.get(
    "",
    response_model=sync.TaskListPage,
    summary="列出无项目任务",
    description="按最近活动位置分页读取本人无项目任务摘要，默认 50 条；仅首页返回同步基线。",
)
async def list_standalone_tasks(
    response: Response,
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page, _services(server), user.id, None, limit=limit, page_cursor=page_cursor
    )


@task_router.post(
    "",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="创建任务并发送首条消息",
    description="project_id 可为 null，创建无项目任务；也可指定本人项目。首条消息和任务原子保存，request_id 重试不重复执行。",
)
async def create_task(
    body: tasks.TaskCreate,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await asyncio.to_thread(
        tasks.send, _services(server), user.id, body, project_id=body.project_id
    )
    worker.wake()
    return result


@task_router.get(
    "/sync",
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
    user: User = Depends(current_user),
) -> sync.TaskSyncPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_sync,
        _services(server),
        user.id,
        None,
        sync_cursor=sync_cursor,
        page_cursor=page_cursor,
        limit=limit,
    )


@task_router.get(
    "/{task_id}",
    response_model=tasks.TaskDetail,
    summary="读取任务对话和执行状态",
    description="返回全部用户消息、已保存的模型回复、排队/运行/终态及工具名称。可轮询观察增量回复；不返回凭据、工具参数或客户端文件内容。其他用户的任务返回 404。",
)
async def task_detail(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.detail, _services(server), user.id, task_id)


@task_router.post(
    "/{task_id}/messages",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="继续任务对话",
    description="在同一任务中发送下一轮消息，保留已完成对话上下文，使用最新项目配置。任务执行中返回 409；同一 request_id 的相同消息返回原任务，不重复执行。",
)
async def send_task_message(
    task_id: str,
    body: tasks.TaskMessage,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await asyncio.to_thread(tasks.send, _services(server), user.id, body, task_id=task_id)
    worker.wake()
    return result


@task_router.post(
    "/{task_id}/cancel",
    response_model=tasks.TaskDetail,
    summary="停止当前回复",
    description="取消排队中的消息或请求停止当前运行。已保存的回复和历史保留，已发生的工具操作不会回滚；之后可继续发送消息。重复取消不会产生新执行。",
)
async def cancel_task(
    task_id: str,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.cancel, _services(server), user.id, task_id)
