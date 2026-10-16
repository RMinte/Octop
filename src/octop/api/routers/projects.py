"""HTTP surface for independent projects."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from octop.api.common.content_disposition import content_disposition
from octop.api.deps import current_user, get_server, require_database
from octop.infra.db.services import SharedServices
from octop.infra.errors import ErrorCode, OctopError
from octop.infra.projects import artifacts, service, sync, tasks
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
from octop.infra.utils.locale import resolve_request_locale

_TASK_STREAM_SECONDS = 25.0

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
    summary="列出项目可用的目录、Skill、MCP、插件和子代理角色",
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
    description="必须提供 Idempotency-Key 请求头；同一用户、同一标识和相同内容重试返回首次创建结果，内容不同返回 409。保存两类目录、Skill/MCP、插件和子代理引用、记忆与团队配置及行动指南；不创建专家，不上传或访问客户端文件。同名项目返回 409。",
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
    description="只返回该列表基线之后变化的任务摘要，包含任务 status 和 list_revision，同一任务合并为最终状态，不包含正文。也返回移出当前归档筛选的摘要，调用方应按 archived 字段更新显示范围。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_project_tasks(
    project_id: str,
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
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
        archived=archived,
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
    description="Octop 网页的项目编辑入口。只修改提交的字段；省略保持原值，目录传 null 解除关联，Skill/MCP、插件和子代理列表传 [] 清空。必须提交 expected_revision；版本冲突返回 409。名称、简介、行动指南、能力列表及记忆/团队开关不接受 null。",
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
    description="按最近活动位置分页，包含每个任务的 status 和 list_revision，无需逐个读取详情。默认 50 条；仅首页返回独立同步基线，不读取任务正文。",
)
async def list_project_tasks(
    project_id: str,
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page,
        _services(server),
        user.id,
        project_id,
        limit=limit,
        page_cursor=page_cursor,
        archived=archived,
    )


@router.post(
    "/{project_id}/tasks",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="在项目中发送首条消息并创建任务",
    description="保存任务及首条消息后排队执行，返回任务详情。每轮采用发送时的项目配置及本轮 options；不创建专家。request_id 用于幂等重试。模型或资源未配置时不会创建空任务。",
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
    description="按最近活动位置分页读取本人无项目任务摘要，包含每个任务的 status 和 list_revision，无需逐个读取详情。默认 50 条；仅首页返回同步基线。",
)
async def list_standalone_tasks(
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
    limit: int = Query(default=50, ge=1, le=100, description="每页摘要数量，默认 50，最多 100。"),
    page_cursor: str | None = Query(
        default=None, max_length=4096, description="历史任务下一页游标。"
    ),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> sync.TaskListPage:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        sync.task_page,
        _services(server),
        user.id,
        None,
        limit=limit,
        page_cursor=page_cursor,
        archived=archived,
    )


@task_router.post(
    "",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="创建任务并发送首条消息",
    description="project_id 可为 null，创建无项目任务；也可指定本人项目。首条消息和任务原子保存，可用 options 选择本轮模型、Skill、MCP、插件、子代理及记忆/团队设置。request_id 重试不重复执行。",
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
    description="只返回该列表基线之后变化的任务摘要，包含任务 status 和 list_revision，同一任务合并为最终状态，不包含正文。也返回移出当前归档筛选的摘要，调用方应按 archived 字段更新显示范围。增量分页固定截止版本，最后一页才返回新同步位置。无效游标返回 409 PROJECT_SYNC_RESET。",
)
async def sync_standalone_tasks(
    response: Response,
    archived: bool = Query(
        default=False, description="false 读取未归档任务，true 读取归档任务；两种游标不可混用。"
    ),
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
        archived=archived,
    )


@task_router.get(
    "/resources",
    response_model=tasks.TaskResources,
    summary="读取本轮可选择的模型和项目能力",
    description="仅列出可用模型和本人可访问的资源引用，不返回密钥；选择仅影响本轮，不修改项目或全局默认值。",
)
async def task_resources(
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskResources:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.resources, _services(server), user.id)


@task_router.patch(
    "/{task_id}",
    response_model=tasks.TaskDetail,
    summary="修改任务标题或归档状态",
    description="必须提供 expected_revision（list_revision）。归档保留会话，可恢复；执行中的任务须先结束或停止再归档，归档任务须恢复后才能继续发送。版本冲突返回 409。",
)
async def patch_task(
    task_id: str,
    body: tasks.TaskPatch,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.patch, _services(server), user.id, task_id, body)


@task_router.get(
    "/{task_id}",
    response_model=tasks.TaskDetail,
    summary="读取任务对话和执行状态",
    description="返回最近一页轮次及任务摘要，默认 50 轮、最多 100；page_cursor 读取更早记录。每轮 artifacts 包含已发布附件元数据，内容通过鉴权 HTTP 下载。首页 event_cursor 可用于订阅实时状态，不返回凭据或附件字节。其他用户的任务返回 404。",
)
async def task_detail(
    task_id: str,
    response: Response,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: str | None = Query(default=None, min_length=1, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(
        tasks.detail, _services(server), user.id, task_id, limit=limit, page_cursor=page_cursor
    )


@task_router.get(
    "/{task_id}/artifacts/{artifact_id}/download",
    response_class=Response,
    summary="下载任务已发布的附件",
    description="需要当前任务所属账号鉴权。下载发布时保存的不可变快照，不读取工作目录，不依赖任务运行状态。不存在或他人附件返回 404；响应始终为 attachment，支持中文文件名。",
    responses={
        200: {
            "description": "附件原始字节，Content-Type 按已保存的 MIME 类型返回。",
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
    },
)
async def download_task_artifact(
    task_id: str,
    artifact_id: str,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> Response:
    artifact, content = await asyncio.to_thread(
        artifacts.download, _services(server), user.id, task_id, artifact_id
    )
    return Response(
        content=content,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": content_disposition(artifact.name),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@task_router.post(
    "/{task_id}/messages",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="继续任务对话",
    description="在同一任务中发送下一轮消息，保留成功历史和标记为未完成的失败/取消正文，使用最新项目配置和本轮 options；不修改项目。归档任务须先恢复。任务执行中返回 409；同一 request_id 的相同消息返回原任务，不重复执行。",
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
    description="必须指定 turn_id，仅取消该轮排队或执行。该轮已结束时幂等返回，不会停止后续轮次；其他任务的轮次返回 404。已保存正文保留，工具操作不回滚。",
)
async def cancel_task(
    task_id: str,
    body: tasks.TaskCancel,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    response.headers["Cache-Control"] = "no-store"
    return await asyncio.to_thread(tasks.cancel, _services(server), user.id, task_id, body.turn_id)


@task_router.post(
    "/{task_id}/turns/{turn_id}/retry",
    response_model=tasks.TaskDetail,
    status_code=202,
    summary="重新尝试最后一个失败或停止的轮次",
    description="使用原消息和当前项目配置创建新一轮，保留原记录。仅允许重试最后一个失败/取消轮次；同一 request_id 重传不重复执行。之前工具操作不会回滚，重试可能再次执行操作。",
)
async def retry_task_turn(
    task_id: str,
    turn_id: str,
    body: tasks.TaskRetry,
    response: Response,
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> tasks.TaskDetail:
    worker = _task_worker(server)
    response.headers["Cache-Control"] = "no-store"
    result = await asyncio.to_thread(
        tasks.send, _services(server), user.id, body, task_id=task_id, retry_of_turn_id=turn_id
    )
    worker.wake()
    return result


async def _task_events(
    request: Request,
    services: SharedServices,
    owner_id: int,
    task_id: str,
    cursor: str,
    pending: list[tasks.TaskUpdate],
    snapshot: tasks.TaskDetail | None = None,
) -> AsyncIterator[str]:
    started = time.monotonic()
    heartbeat = started
    if snapshot is not None:
        yield f"id: {cursor}\nevent: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
    # Rotate connections to recheck authentication and apply renewed access tokens.
    while time.monotonic() - started < _TASK_STREAM_SECONDS:
        if await request.is_disconnected():
            return
        for update in pending:
            cursor = update.cursor
            yield f"id: {cursor}\nevent: turn\ndata: {update.model_dump_json()}\n\n"
        if time.monotonic() - heartbeat >= 10:
            yield ": heartbeat\n\n"
            heartbeat = time.monotonic()
        await asyncio.sleep(0.5)
        try:
            pending = await asyncio.to_thread(tasks.updates, services, owner_id, task_id, cursor)
        except OctopError as exc:
            yield (
                "event: error\ndata: "
                + json.dumps(exc.to_envelope(locale=resolve_request_locale(request)))
                + "\n\n"
            )
            return


@task_router.get(
    "/{task_id}/events",
    response_class=StreamingResponse,
    summary="订阅任务轮次的实时状态",
    description="Bearer 鉴权的 SSE。使用首页 event_cursor 或 Last-Event-ID 续接；turn 事件的 data 为 {task: TaskSummary, turn: TaskTurn, cursor: string}，SSE id 与 cursor 一致；snapshot 为 TaskDetail。同一轮中间状态可合并。断线后补齐遗漏轮次的最新状态，不重放工具操作。无游标先返回最近一页 snapshot。游标失效返回 409 PROJECT_SYNC_RESET，需重建首页。连接约 25 秒后结束，可用最后游标重连。",
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def task_events(
    task_id: str,
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=4096),
    last_event_id: str | None = Header(default=None, max_length=4096),
    server: OctopServer = Depends(get_server),
    user: User = Depends(current_user),
) -> StreamingResponse:
    services = _services(server)
    position = cursor or last_event_id
    snapshot = None
    if position is None:
        snapshot = await asyncio.to_thread(tasks.detail, services, user.id, task_id)
        position = snapshot.event_cursor
    assert position is not None
    pending = await asyncio.to_thread(tasks.updates, services, user.id, task_id, position)
    return StreamingResponse(
        _task_events(request, services, user.id, task_id, position, pending, snapshot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
